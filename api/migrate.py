"""Idempotent database migrations for the TWAIN API.

Applies every ``migrations/*.sql`` file (in filename order) inside a Postgres
*advisory lock* so concurrently-starting API tasks can't race, and records each
applied file in a ``schema_migrations`` ledger so re-runs skip work. The SQL
files are themselves idempotent (``CREATE TABLE IF NOT EXISTS`` …), so running
this is always safe — even against a database that predates the ledger.

Two entry points:

* **On API startup** — ``main.py``'s lifespan calls :func:`apply_migrations`, so
  a fresh deploy migrates its own database with no manual step. Disable with
  ``RUN_MIGRATIONS_ON_STARTUP=false`` (e.g. to run migrations as a one-off task).
* **As a CLI** — ``python -m migrate`` / ``python migrate.py --dry-run`` for a
  bastion / one-off ECS task, or locally.

Credentials are resolved exactly like the rest of the API (``database.py``): from
``DB_PASSWORD`` locally, or AWS Secrets Manager when ``AWS_SECRET_ARN`` is set.
"""
import logging
from pathlib import Path

from database import get_connection

logger = logging.getLogger("twain.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# A fixed key so every API instance contends on the *same* advisory lock while
# migrating (any constant works; this one is arbitrary but stable).
_LOCK_KEY = 728_411_001


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Every ``.sql`` migration, in the order they must run (filename sort).

    Files are named with a zero-padded numeric prefix (``001_…``, ``002_…``) so a
    plain lexicographic sort is the apply order.
    """
    return sorted(directory.glob("*.sql"))


def _ensure_ledger(cur) -> None:
    """Create the ledger that records which migrations have been applied."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


def _already_applied(cur) -> set[str]:
    cur.execute("SELECT filename FROM schema_migrations;")
    return {row[0] for row in cur.fetchall()}


def apply_migrations(*, dry_run: bool = False) -> list[str]:
    """Apply any not-yet-applied migrations; return the filenames applied.

    All work happens under a Postgres advisory lock, so multiple API instances
    starting at once serialize instead of racing. Each file runs in its own
    transaction and is recorded in ``schema_migrations`` on success; a failure
    rolls that file back and re-raises (nothing partial is left committed).
    """
    files = discover_migrations()
    if not files:
        logger.warning("no migration files found in %s", MIGRATIONS_DIR)
        return []

    conn = get_connection()
    applied: list[str] = []
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # Session-scoped lock: held across the commits below until we unlock.
            cur.execute("SELECT pg_advisory_lock(%s);", (_LOCK_KEY,))
            _ensure_ledger(cur)
            conn.commit()

            done = _already_applied(cur)
            for path in files:
                if path.name in done:
                    logger.info("migrate: %s already applied — skipping", path.name)
                    continue
                if dry_run:
                    logger.info("migrate: would apply %s", path.name)
                    applied.append(path.name)
                    continue
                logger.info("migrate: applying %s", path.name)
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s) "
                    "ON CONFLICT (filename) DO NOTHING;",
                    (path.name,),
                )
                conn.commit()
                applied.append(path.name)
        return applied
    except Exception:
        conn.rollback()
        raise
    finally:
        # Best-effort unlock + close; a dropped connection releases the lock anyway.
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s);", (_LOCK_KEY,))
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - never mask the original error
            logger.debug("advisory-unlock cleanup failed (ignored): %s", exc)
        conn.close()


def _main(argv=None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Apply TWAIN database migrations.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be applied; make no changes",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="list discovered migration files (no DB connection) and exit",
    )
    args = parser.parse_args(argv)

    if args.list:
        for f in discover_migrations():
            print(f.name)
        return 0

    applied = apply_migrations(dry_run=args.dry_run)
    if applied:
        verb = "Would apply" if args.dry_run else "Applied"
        print(f"{verb}: {', '.join(applied)}")
    else:
        print("Database is up to date; nothing to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
