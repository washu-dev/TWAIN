"""Database access for the TWAIN API.

Connection properties are sourced from AWS Secrets Manager under the
``TWAIN/database/*`` group (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD).

Access model:
- In AWS (ECS/Fargate), the task role should be ``TWAIN-secrets-reader`` (or a
  role with equivalent access), so boto3's default credentials can read + decrypt
  the secrets directly.
- Locally, set ``TWAIN_SECRETS_ROLE_ARN`` to the reader role ARN; this module
  assumes it before reading, matching the least-privilege access design.
- For offline development without AWS, set ``TWAIN_DB_FROM_ENV=true`` to read the
  same DB_* values from the environment/.env instead.
"""

import os
from functools import cache, lru_cache

import boto3
import psycopg2
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from psycopg2.extras import Json, RealDictCursor

load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
# Prefix grouping the connection secrets, e.g. TWAIN/database/DB_HOST.
SECRET_PREFIX = os.getenv("TWAIN_SECRET_PREFIX", "TWAIN/database")
# Optional: assume this role before reading secrets (for local/cross-account use).
SECRETS_ROLE_ARN = os.getenv("TWAIN_SECRETS_ROLE_ARN")
# Escape hatch for offline dev: read DB_* from the environment instead of AWS.
_USE_ENV = os.getenv("TWAIN_DB_FROM_ENV", "").strip().lower() in ("1", "true", "yes")

# Connection property name => environment-variable fallback name.
_DB_KEYS = {
    "host": "DB_HOST",
    "port": "DB_PORT",
    "dbname": "DB_NAME",
    "user": "DB_USER",
    "password": "DB_PASSWORD",
}


def _secrets_client():
    """Secrets Manager client, optionally using assumed-role credentials."""
    if SECRETS_ROLE_ARN:
        sts = boto3.client("sts", region_name=AWS_REGION)
        creds = sts.assume_role(
            RoleArn=SECRETS_ROLE_ARN,
            RoleSessionName="twain-api",
        )["Credentials"]
        return boto3.client(
            "secretsmanager",
            region_name=AWS_REGION,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
    return boto3.client("secretsmanager", region_name=AWS_REGION)


@cache
def get_secret(secret_id: str) -> str:
    """Read a single ``SecretString`` from Secrets Manager (cached per id).

    Used for config identifiers stored outside the DB group — e.g. the public
    Entra tenant/app ids under ``TWAIN/sso/*`` that ``auth.py`` needs — using the
    same task-role / assume-role credentials as the DB secrets. Raises on failure
    so callers can decide whether a missing secret is fatal.
    """
    return _secrets_client().get_secret_value(SecretId=secret_id)["SecretString"]


@lru_cache(maxsize=1)
def _load_db_config() -> dict:
    """Resolve DB connection properties once and cache them for the process."""
    if _USE_ENV:
        return {
            prop: os.getenv(env, "")
            for prop, env in _DB_KEYS.items()
        }

    try:
        client = _secrets_client()
        return {
            prop: client.get_secret_value(SecretId=f"{SECRET_PREFIX}/{env}")[
                "SecretString"
            ]
            for prop, env in _DB_KEYS.items()
        }
    except (ClientError, BotoCoreError) as e:
        raise RuntimeError(
            f"Failed to load DB connection secrets from '{SECRET_PREFIX}/*': {e}"
        ) from e


def read_secret(secret_id: str) -> str:
    """Read a single Secrets Manager secret string by its full id.

    For features that store one opaque value (e.g. the GitHub issue PAT) rather
    than the grouped DB connection properties. Thin wrapper over the cached
    :func:`get_secret` that raises a friendly ``RuntimeError`` on failure instead
    of the raw boto exception.
    """
    try:
        return get_secret(secret_id)
    except (ClientError, BotoCoreError) as e:
        raise RuntimeError(f"Failed to load secret '{secret_id}': {e}") from e


def get_connection():
    """Create and return a database connection using the resolved credentials."""
    return psycopg2.connect(**_load_db_config())


ALLOWED_ROLES = ("user", "admin")


def upsert_user(subject: str, email: str, name: str, *, bootstrap_admin: bool = False) -> dict:
    """Insert or refresh a user by Entra subject; return the stored row.

    ``bootstrap_admin`` promotes the row to admin on first insert only (used for
    seed admins / the dev identity); it never demotes an existing user.
    """
    role = "admin" if bootstrap_admin else "user"
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            INSERT INTO users (subject, email, name, role, last_login_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (subject) DO UPDATE
              SET email = EXCLUDED.email,
                  name = EXCLUDED.name,
                  last_login_at = now()
            RETURNING id, subject, email, name, role, created_at, last_login_at,
                      notify_prefs;
            """,
            (subject, email, name, role),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()
        return row
    except Exception as e:
        raise Exception(f"Failed to upsert user: {e}") from e


def list_users() -> list:
    """Return all users, newest first."""
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT id, subject, email, name, role, created_at, last_login_at "
            "FROM users ORDER BY created_at DESC;"
        )
        results = cursor.fetchall()
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        raise Exception(f"Failed to list users: {e}") from e


def set_notify_prefs(user_id: str, prefs: dict) -> dict | None:
    """Store a user's notification preferences; return them, or None if no user.

    ``prefs`` is the whole preferences object (the Settings page sends its full
    state): ``{"enabled": bool, "kinds": {kind: bool, ...}}``. The runner treats
    any missing key as "send", so '{}' means all notifications on.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "UPDATE users SET notify_prefs = %s WHERE id = %s "
            "RETURNING notify_prefs;",
            (Json(prefs), user_id),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()
        return row["notify_prefs"] if row else None
    except Exception as e:
        raise Exception(f"Failed to set notification preferences: {e}") from e


def set_user_role(user_id: str, role: str) -> dict | None:
    """Set a user's role; return the updated row, or None if no such user."""
    if role not in ALLOWED_ROLES:
        raise ValueError(f"role must be one of {ALLOWED_ROLES}")
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "UPDATE users SET role = %s WHERE id = %s "
            "RETURNING id, subject, email, name, role, created_at, last_login_at;",
            (role, user_id),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()
        return row
    except Exception as e:
        raise Exception(f"Failed to set user role: {e}") from e
