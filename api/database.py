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
from functools import lru_cache

import boto3
import psycopg2
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

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


def get_connection():
    """Create and return a database connection using the resolved credentials."""
    return psycopg2.connect(**_load_db_config())


def query_greetings():
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT message FROM greetings;")
        results = cursor.fetchall()
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        raise Exception(f"Database query failed: {e}") from e
