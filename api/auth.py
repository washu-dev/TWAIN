"""Authentication & authorization for the TWAIN API.

Users sign in with Microsoft Entra ID (Azure AD) — WashU's SSO, the same tenant
the engine uses for the LLM gateway. The frontend runs the OIDC auth-code + PKCE
flow and sends the resulting access token as a Bearer header; here we validate it
(signature via the tenant JWKS, plus ``aud`` / ``iss`` / ``exp``), then upsert a
local ``users`` row keyed by the token's stable subject (``oid``). Admin rights
are a local ``users.role`` flag, so admins can promote each other in-app without
needing Azure directory permissions.

Config comes from environment variables (values via Secrets Manager / ``.env``):

* ``ENTRA_TENANT_ID``        WashU tenant (GUID).
* ``ENTRA_API_AUDIENCE``     App ID URI / client id of the API app registration.
* ``ENTRA_ISSUER``           Optional; derived from the tenant when unset.
* ``AUTH_DISABLED``          "true" for local dev — injects a dev admin identity.
* ``BOOTSTRAP_ADMIN_EMAILS`` Optional comma-separated seed admins on first login.
"""
import os
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from database import upsert_user

TENANT_ID = os.getenv("ENTRA_TENANT_ID", "")
API_AUDIENCE = os.getenv("ENTRA_API_AUDIENCE", "")
ISSUER = os.getenv("ENTRA_ISSUER") or (
    f"https://login.microsoftonline.com/{TENANT_ID}/v2.0" if TENANT_ID else ""
)
JWKS_URL = (
    f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
    if TENANT_ID
    else ""
)
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "").lower() in {"1", "true", "yes"}
BOOTSTRAP_ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("BOOTSTRAP_ADMIN_EMAILS", "").split(",")
    if e.strip()
}

_bearer = HTTPBearer(auto_error=False)
_jwks_client: jwt.PyJWKClient | None = None


def _get_jwks_client() -> jwt.PyJWKClient:
    """Lazily build (and cache) the JWKS client so import never needs network/config."""
    global _jwks_client
    if _jwks_client is None:
        if not JWKS_URL:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Auth not configured: ENTRA_TENANT_ID is unset.",
            )
        _jwks_client = jwt.PyJWKClient(JWKS_URL)
    return _jwks_client


def verify_token(token: str) -> dict:
    """Validate an Entra access token and return its claims, or raise 401."""
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=API_AUDIENCE,
            issuer=ISSUER,
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


async def get_current_user(
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
    subject, email, name = _identity_from_claims(verify_token(creds.credentials))
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
