import json
import os

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "twain_db")
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
