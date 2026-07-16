import json
import os

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "twaindb")
DB_USER = os.getenv("DB_USER", "postgres")


def _get_secret_from_aws(secret_arn: str) -> str:
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1"))
    try:
        response = client.get_secret_value(SecretId=secret_arn)
        secret = response.get("SecretString", "")
        try:
            return json.loads(secret).get("password", secret)
        except json.JSONDecodeError:
            return secret
    except ClientError as e:
        raise Exception(f"Failed to retrieve secret from Secrets Manager: {e}") from e


def _resolve_db_password() -> str:
    """Use Secrets Manager in AWS, fall back to .env locally."""
    secret_arn = os.getenv("AWS_SECRET_ARN")
    if secret_arn:
        return _get_secret_from_aws(secret_arn)
    return os.getenv("DB_PASSWORD", "")


def get_connection():
    """Create and return a database connection, resolving credentials on demand."""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=_resolve_db_password(),
    )


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
            RETURNING id, subject, email, name, role, created_at, last_login_at;
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
