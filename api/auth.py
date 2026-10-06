"""Authentication & authorization for the TWAIN API.

Users sign in with Microsoft Entra ID (Azure AD) — WashU's SSO, the same tenant
the engine uses for the LLM gateway. The frontend runs the OIDC auth-code + PKCE
flow and sends the resulting access token as a Bearer header; here we validate it
(signature via the tenant JWKS, plus ``aud`` / ``iss`` / ``exp``), then upsert a
local ``users`` row keyed by the token's stable subject (``oid``). Admin rights
are a local ``users.role`` flag, so admins can promote each other in-app without
needing Azure directory permissions.

Config is resolved lazily (env var first, then AWS Secrets Manager under
``TWAIN/sso/*`` via the task role — the same mechanism database.py uses):

* ``ENTRA_TENANT_ID``        WashU tenant (GUID).            → ``TWAIN/sso/TENANT_ID``
* ``ENTRA_API_AUDIENCE``     Accepted token audience: this registration's app
                             (client) id. It is the ``aud`` of both the SPA's ID
                             token and its own-API v2 access token, so one value
                             covers ID-token and access-token mode. → ``TWAIN/sso/APP_ID``
* ``ENTRA_ISSUER``           Optional; derived from the tenant when unset.
* ``AUTH_DISABLED``          "true" for local dev — injects a dev admin identity.
* ``BOOTSTRAP_ADMIN_EMAILS`` Optional comma-separated seed admins on first login.

Until Entra SSO is wired on the frontend, a lightweight **interim** email login
(``POST /api/auth/login``) mints a short-lived HS256 session token so the app has
real per-user identity. It is enabled only when a signing secret is configured
(``INTERIM_JWT_SECRET``); both token types are accepted here, routed by algorithm.
See ``docs/project/WEB_MVP_DELIVERY_PLAN.md`` (Phase 1). Interim config:

* ``INTERIM_JWT_SECRET``     Enables interim login; the HS256 signing key.
* ``INTERIM_ALLOWED_DOMAINS`` Comma-separated email domains allowed (default ``wustl.edu``).
* ``INTERIM_ALLOWED_EMAILS`` Optional comma-separated individual emails allowed.
* ``INTERIM_TOKEN_TTL_HOURS`` Session token lifetime (default ``12``).
"""
import os
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from database import get_secret, upsert_user

# Prefix grouping the public SSO identifiers, e.g. TWAIN/sso/TENANT_ID.
SSO_SECRET_PREFIX = os.getenv("TWAIN_SSO_SECRET_PREFIX", "TWAIN/sso")


def _read_sso(key: str) -> str:
    """Read one SSO identifier from Secrets Manager; "" if unavailable."""
    try:
        return get_secret(f"{SSO_SECRET_PREFIX}/{key}").strip()
    except Exception:  # noqa: BLE001 — treat any read failure as "not configured"
        return ""


@lru_cache(maxsize=1)
def _sso_config() -> tuple[str, str]:
    """Resolve ``(tenant_id, api_audience)`` — env var first, then Secrets Manager.

    Raises 500 when neither source yields both values. The exception is not cached
    (``lru_cache`` only caches returns), so a later request retries the lookup.
    """
    tenant = os.getenv("ENTRA_TENANT_ID", "").strip() or _read_sso("TENANT_ID")
    audience = os.getenv("ENTRA_API_AUDIENCE", "").strip() or _read_sso("APP_ID")
    if not tenant or not audience:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Auth not configured: set ENTRA_TENANT_ID + ENTRA_API_AUDIENCE, "
            f"or provide {SSO_SECRET_PREFIX}/TENANT_ID and {SSO_SECRET_PREFIX}/APP_ID "
            "in Secrets Manager.",
        )
    return tenant, audience


AUTH_DISABLED = os.getenv("AUTH_DISABLED", "").lower() in {"1", "true", "yes"}
BOOTSTRAP_ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("BOOTSTRAP_ADMIN_EMAILS", "").split(",")
    if e.strip()
}

# ── Interim email login (pre-SSO) ─────────────────────────────────────────────
INTERIM_JWT_SECRET = os.getenv("INTERIM_JWT_SECRET", "")
INTERIM_ISSUER = "twain-interim"
INTERIM_AUDIENCE = "twain-web"
INTERIM_TOKEN_TTL_HOURS = float(os.getenv("INTERIM_TOKEN_TTL_HOURS", "12"))
INTERIM_ALLOWED_DOMAINS = {
    d.strip().lower().lstrip("@")
    for d in os.getenv("INTERIM_ALLOWED_DOMAINS", "wustl.edu").split(",")
    if d.strip()
}
INTERIM_ALLOWED_EMAILS = {
    e.strip().lower()
    for e in os.getenv("INTERIM_ALLOWED_EMAILS", "").split(",")
    if e.strip()
}

_bearer = HTTPBearer(auto_error=False)
_jwks_client: jwt.PyJWKClient | None = None


def _get_jwks_client() -> jwt.PyJWKClient:
    """Lazily build (and cache) the JWKS client so import never needs network/config."""
    global _jwks_client
    if _jwks_client is None:
        tenant, _ = _sso_config()  # raises 500 if unconfigured
        _jwks_client = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
        )
    return _jwks_client


def verify_token(token: str) -> dict:
    """Validate an Entra ID/access token and return its claims, or raise 401."""
    tenant, audience = _sso_config()  # raises 500 if unconfigured
    issuer = (
        os.getenv("ENTRA_ISSUER")
        or f"https://login.microsoftonline.com/{tenant}/v2.0"
    )
    # Accept both the bare app id and its api:// URI form: an ID token carries the
    # client id as `aud`, while a v2 own-API access token may carry either form.
    accepted_audiences = (
        [audience]
        if audience.startswith("api://")
        else [audience, f"api://{audience}"]
    )
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=accepted_audiences,
            issuer=issuer,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc


def _identity_from_claims(claims: dict) -> tuple[str, str, str]:
    """Pull (subject, email, name) out of Entra claims."""
    subject = claims.get("oid") or claims.get("sub") or ""
    email = (
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("upn")
        or ""
    ).lower()
    name = claims.get("name") or email
    return subject, email, name


def interim_auth_available() -> bool:
    """True when interim email login is configured (a signing secret is set)."""
    return bool(INTERIM_JWT_SECRET)


def email_allowed(email: str) -> bool:
    """Whether an email may use interim login (explicit allowlist or allowed domain)."""
    email = email.strip().lower()
    if not email or "@" not in email:
        return False
    if email in INTERIM_ALLOWED_EMAILS:
        return True
    return email.rsplit("@", 1)[-1] in INTERIM_ALLOWED_DOMAINS


def mint_interim_token(user: dict) -> str:
    """Issue a short-lived HS256 session token for an authenticated interim user."""
    now = datetime.now(UTC)
    claims = {
        "sub": user["subject"],
        "email": user.get("email", ""),
        "role": user.get("role", "user"),
        "iss": INTERIM_ISSUER,
        "aud": INTERIM_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=INTERIM_TOKEN_TTL_HOURS),
    }
    return jwt.encode(claims, INTERIM_JWT_SECRET, algorithm="HS256")


def verify_interim_token(token: str) -> dict:
    """Validate an interim session token and return its claims, or raise 401."""
    if not INTERIM_JWT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Interim auth is not configured.",
        )
    try:
        return jwt.decode(
            token,
            INTERIM_JWT_SECRET,
            algorithms=["HS256"],
            audience=INTERIM_AUDIENCE,
            issuer=INTERIM_ISSUER,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc


def _decode_bearer(token: str) -> dict:
    """Verify a bearer token, routing by algorithm: HS256 → interim, RS256 → Entra.

    Each verifier pins its own algorithm, so an HS256 token can never be checked
    against the Entra RSA public key (avoiding the classic alg-confusion attack).
    """
    try:
        alg = jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc
    if alg == "HS256":
        return verify_interim_token(token)
    return verify_token(token)


def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> dict:
    """Resolve the authenticated user, creating/refreshing their ``users`` row."""
    if AUTH_DISABLED:
        return upsert_user("dev-user", "dev@wustl.edu", "Dev User", bootstrap_admin=True)

    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )
    subject, email, name = _identity_from_claims(_decode_bearer(creds.credentials))
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is missing a subject (oid/sub).",
        )
    return upsert_user(
        subject, email, name, bootstrap_admin=email in BOOTSTRAP_ADMIN_EMAILS
    )


def require_admin(user: Annotated[dict, Depends(get_current_user)]) -> dict:
    """Dependency that allows only users with the admin role."""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required.",
        )
    return user


# Reusable dependency aliases for route handlers (FastAPI Annotated style).
CurrentUser = Annotated[dict, Depends(get_current_user)]
AdminUser = Annotated[dict, Depends(require_admin)]
