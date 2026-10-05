#!/usr/bin/env python3
"""TWAIN deployment readiness check — read-only, changes nothing.

Tells you exactly what is (and isn't) ready before you deploy or run.

    python scripts/preflight.py            # local: env, DB, migrations, LLM creds, auth
    python scripts/preflight.py --aws      # also verify the AWS resources exist (boto3)

Exit code 0 = every REQUIRED check passed; 1 = something required is missing.
Warnings (⚠) never fail the run; they flag things that are optional or only
matter in certain modes.

The AWS checks are all read-only `describe`/`get` calls. The database
connectivity check runs only in local mode — a deployed RDS instance is private,
so it's validated in-VPC by the API's own startup migration instead.
"""
import argparse
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

OK, BAD, WARN, SKIP = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[33m⚠\033[0m", "·"

# Tables the migrations create; their presence means the schema is applied.
EXPECTED_TABLES = (
    "users", "conversations", "messages", "sessions",
    "run_events", "jobs", "artifacts", "schema_migrations",
)

# Deploy targets (mirror the GitHub workflows + ECS task definitions).
ECR_REPOS = ("twain-ecr", "twain-runner-ecr")
ECS_CLUSTER = "twain-cluster"
ECS_SERVICES = ("twain-api", "twain-runner")
LOG_GROUPS = ("/ecs/twain-api", "/ecs/twain-runner")
TASK_DEFS = ("api/ecs-task-definition.json", "runner/ecs-task-definition.json")


class Report:
    def __init__(self):
        self.failed = 0
        self.warned = 0

    def section(self, title):
        print(f"\n\033[1m{title}\033[0m")

    def ok(self, msg):
        print(f"  {OK} {msg}")

    def bad(self, msg, hint=None):
        print(f"  {BAD} {msg}")
        if hint:
            print(f"      → {hint}")
        self.failed += 1

    def warn(self, msg, hint=None):
        print(f"  {WARN} {msg}")
        if hint:
            print(f"      → {hint}")
        self.warned += 1

    def skip(self, msg):
        print(f"  {SKIP} {msg}")


def load_env() -> None:
    """Load the repo-root .env so we see the same config the services do."""
    env_path = REPO / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
        return
    except Exception:  # noqa: BLE001 - fall back to a tiny hand parser
        pass
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


# ── local checks ──────────────────────────────────────────────────────────────

def check_env(rep: Report) -> None:
    rep.section("Configuration (.env / environment)")
    for var in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER"):
        (rep.ok if os.getenv(var) else rep.bad)(f"{var}={os.getenv(var) or '(unset)'}")
    if os.getenv("DB_PASSWORD") or os.getenv("AWS_SECRET_ARN"):
        rep.ok("database password source set (DB_PASSWORD or AWS_SECRET_ARN)")
    else:
        rep.bad("no DB password source", "set DB_PASSWORD locally, or AWS_SECRET_ARN in cloud")

    for var in ("API_KEY", "CLIENT_ID", "CLIENT_SECRET"):
        if os.getenv(var):
            rep.ok(f"{var} present")
        else:
            rep.warn(f"{var} unset", "required by the runner; in cloud it comes from Secrets Manager")


def check_auth(rep: Report) -> None:
    rep.section("Auth configuration")
    if os.getenv("AUTH_DISABLED", "").lower() in {"1", "true", "yes"}:
        rep.warn("AUTH_DISABLED=true — dev only", "must be unset/false in any shared or cloud deploy")
        return
    if os.getenv("ENTRA_TENANT_ID") and os.getenv("ENTRA_API_AUDIENCE"):
        rep.ok("Entra SSO configured (ENTRA_TENANT_ID + ENTRA_API_AUDIENCE)")
    elif os.getenv("INTERIM_JWT_SECRET"):
        rep.ok("interim email login enabled (INTERIM_JWT_SECRET)")
        rep.warn("using interim login, not Entra SSO", "fine for a pilot; wire ENTRA_* for production")
    else:
        rep.bad(
            "no auth method configured",
            "set ENTRA_TENANT_ID + ENTRA_API_AUDIENCE (SSO) or INTERIM_JWT_SECRET (interim login)",
        )


def check_database(rep: Report) -> None:
    rep.section("Database (local connectivity + migrations)")
    try:
        import psycopg2
    except ImportError:
        rep.bad("psycopg2 not importable", "run inside the pixi env or the api venv")
        return

    password = os.getenv("DB_PASSWORD", "")
    if os.getenv("AWS_SECRET_ARN") and not password:
        rep.skip("AWS_SECRET_ARN set — skipping direct connect (RDS is private; API migrates in-VPC)")
        return
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
            dbname=os.getenv("DB_NAME", "twaindb"), user=os.getenv("DB_USER", "postgres"),
            password=password, connect_timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        rep.bad(f"cannot connect to Postgres: {exc}", "is the DB up? (./dev.sh starts one locally)")
        return
    rep.ok("connected to Postgres")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public';"
            )
            present = {r[0] for r in cur.fetchall()}
        missing = [t for t in EXPECTED_TABLES if t not in present]
        if missing:
            rep.bad(
                f"missing tables: {', '.join(missing)}",
                "apply migrations:  python api/migrate.py   (or start the API once)",
            )
        else:
            rep.ok(f"all {len(EXPECTED_TABLES)} expected tables present (migrations applied)")
    finally:
        conn.close()


# ── AWS checks (read-only) ──────────────────────────────────────────────────────

def _secret_arns_from_task_defs() -> list[tuple[str, str]]:
    """Every (label, secret-ARN) referenced by the ECS task definitions."""
    arns: list[tuple[str, str]] = []
    for rel in TASK_DEFS:
        path = REPO / rel
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for container in data.get("containerDefinitions", []):
            for env in container.get("environment", []):
                if env.get("name") == "AWS_SECRET_ARN":
                    arns.append((f"{rel}:DB password", env["value"]))
            for sec in container.get("secrets", []):
                arns.append((f"{rel}:{sec['name']}", sec["valueFrom"]))
    return arns


def check_aws(rep: Report) -> None:
    rep.section("AWS resources (read-only)")
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        rep.bad("boto3 not importable", "pip install boto3 / run in the pixi env")
        return

    region = os.getenv("AWS_REGION", "us-east-1")
    try:
        ident = boto3.client("sts", region_name=region).get_caller_identity()
        rep.ok(f"AWS credentials OK (account {ident['Account']}, region {region})")
    except (BotoCoreError, ClientError) as exc:
        rep.bad(f"no usable AWS credentials: {exc}", "configure AWS CLI creds, then re-run --aws")
        return

    ecr = boto3.client("ecr", region_name=region)
    for repo in ECR_REPOS:
        try:
            ecr.describe_repositories(repositoryNames=[repo])
            rep.ok(f"ECR repo '{repo}' exists")
        except (BotoCoreError, ClientError):
            rep.bad(f"ECR repo '{repo}' missing", "scripts/aws/provision.sh --apply creates it")

    logs = boto3.client("logs", region_name=region)
    for group in LOG_GROUPS:
        try:
            resp = logs.describe_log_groups(logGroupNamePrefix=group)
            names = {g["logGroupName"] for g in resp.get("logGroups", [])}
            (rep.ok if group in names else rep.bad)(
                f"log group '{group}' {'exists' if group in names else 'missing'}"
            )
        except (BotoCoreError, ClientError) as exc:
            rep.bad(f"log group '{group}' check failed: {exc}")

    ecs = boto3.client("ecs", region_name=region)
    try:
        clusters = ecs.describe_clusters(clusters=[ECS_CLUSTER]).get("clusters", [])
        active = any(c["status"] == "ACTIVE" for c in clusters)
        (rep.ok if active else rep.bad)(
            f"ECS cluster '{ECS_CLUSTER}' {'active' if active else 'missing/inactive'}"
        )
        if active:
            svc = ecs.describe_services(cluster=ECS_CLUSTER, services=list(ECS_SERVICES))
            found = {s["serviceName"]: s["status"] for s in svc.get("services", [])}
            for name in ECS_SERVICES:
                if found.get(name) == "ACTIVE":
                    rep.ok(f"ECS service '{name}' active")
                else:
                    rep.bad(
                        f"ECS service '{name}' {found.get(name, 'missing')}",
                        "create it once (scripts/aws/provision.sh); deploys then update it",
                    )
    except (BotoCoreError, ClientError) as exc:
        rep.bad(f"ECS check failed: {exc}")

    sm = boto3.client("secretsmanager", region_name=region)
    for label, arn in _secret_arns_from_task_defs():
        if arn.endswith("-REPLACE"):
            rep.bad(f"{label}: placeholder ARN not replaced ({arn})",
                    "terraform apply in terraform/, then scripts/aws/setup_secrets.sh --apply to wire the ARNs")
            continue
        try:
            sm.describe_secret(SecretId=arn)
            rep.ok(f"{label}: secret resolves")
        except (BotoCoreError, ClientError):
            rep.bad(f"{label}: secret not found ({arn})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--aws", action="store_true", help="also verify AWS resources exist (boto3)")
    args = parser.parse_args(argv)

    load_env()
    rep = Report()
    print("\033[1mTWAIN preflight\033[0m — read-only readiness check")
    check_env(rep)
    check_auth(rep)
    check_database(rep)
    if args.aws:
        check_aws(rep)
    else:
        rep.section("AWS resources")
        rep.skip("skipped (pass --aws to check ECR / ECS / log groups / secrets)")

    print()
    if rep.failed:
        print(f"\033[31m{rep.failed} required check(s) failed\033[0m", end="")
        print(f", {rep.warned} warning(s)." if rep.warned else ".")
        return 1
    print("\033[32mAll required checks passed\033[0m", end="")
    print(f" ({rep.warned} warning(s)):" if rep.warned else ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
