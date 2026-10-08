import importlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

# Put the repo root on sys.path so `import runner.*` resolves under pytest.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

REPO = Path(__file__).resolve().parents[2]


def pg_available() -> bool:
    try:
        return subprocess.run(["pg_isready", "-h", "localhost"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


@pytest.fixture
def pg_actions(monkeypatch):
    """A throwaway database with every migration applied; yields (RunnerDB, the API's
    email_actions module) wired to it."""
    name = f"twain_actions_test_{uuid.uuid4().hex[:8]}"
    pg = {**os.environ, "PGGSSENCMODE": "disable"}
    subprocess.run(["createdb", "-h", "localhost", name], check=True, env=pg)
    for f in sorted((REPO / "api" / "migrations").glob("*.sql")):
        subprocess.run(["psql", "-h", "localhost", "-d", name, "-q", "-v", "ON_ERROR_STOP=1",
                        "-f", str(f)], check=True, capture_output=True, env=pg)
    user = os.environ.get("USER", "postgres")
    for k, v in {"DB_HOST": "localhost", "DB_NAME": name, "DB_USER": user, "DB_PASSWORD": "",
                 "DB_PORT": "5432", "TWAIN_DB_FROM_ENV": "true", "TWAIN_DISPATCH": "db"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_SECRET_ARN", raising=False)
    from runner import db as runner_db
    monkeypatch.setattr(runner_db, "DB_HOST", "localhost")
    monkeypatch.setattr(runner_db, "DB_NAME", name)
    monkeypatch.setattr(runner_db, "DB_USER", user)
    monkeypatch.syspath_prepend(str(REPO / "api"))
    for mod in ("database", "conversations", "dispatch", "email_actions"):
        sys.modules.pop(mod, None)
    api_actions = importlib.import_module("email_actions")
    db = runner_db.RunnerDB()
    yield db, api_actions
    for mod in ("database", "conversations", "dispatch", "email_actions"):
        sys.modules.pop(mod, None)
    subprocess.run(["dropdb", "-h", "localhost", name], env=pg)

