"""Unit tests for the migration runner — a fake connection stands in for
Postgres, so no database is needed. These check apply/skip/dry-run behaviour and
that the advisory lock is taken and released.
"""
import migrate


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._last_was_ledger_select = False

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        self._last_was_ledger_select = "SELECT filename FROM schema_migrations" in sql

    def fetchall(self):
        if self._last_was_ledger_select:
            return [(name,) for name in self.conn.applied]
        return []


class FakeConn:
    """Records every executed statement; reports `applied` as the ledger contents."""

    def __init__(self, applied=()):
        self.applied = set(applied)
        self.executed = []
        self.autocommit = True
        self.commits = 0
        self.rolledback = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rolledback = True

    def close(self):
        self.closed = True


def _names():
    return [p.name for p in migrate.discover_migrations()]


def _executed_sql(conn):
    return [s for s, _ in conn.executed]


def test_discovers_migrations_in_order():
    names = _names()
    assert names == sorted(names)
    assert "001_web_ui.sql" in names  # the base schema is always present


def test_applies_all_when_ledger_empty(monkeypatch):
    conn = FakeConn(applied=set())
    monkeypatch.setattr(migrate, "get_connection", lambda: conn)

    applied = migrate.apply_migrations()

    assert applied == _names()                      # every migration, in order
    joined = " ".join(_executed_sql(conn))
    assert "pg_advisory_lock" in joined             # serialized against races …
    assert "pg_advisory_unlock" in joined           # … and released afterward
    # each file's body was executed and recorded in the ledger
    for path in migrate.discover_migrations():
        assert path.read_text(encoding="utf-8") in _executed_sql(conn)
    ledger_writes = {
        params[0] for sql, params in conn.executed
        if "INSERT INTO schema_migrations" in sql and params
    }
    assert ledger_writes == set(_names())
    assert conn.closed is True


def test_skips_already_applied(monkeypatch):
    names = _names()
    conn = FakeConn(applied={names[0]})             # first migration already applied
    monkeypatch.setattr(migrate, "get_connection", lambda: conn)

    applied = migrate.apply_migrations()

    assert names[0] not in applied
    assert applied == names[1:]                     # only the rest are applied


def test_dry_run_changes_nothing(monkeypatch):
    conn = FakeConn(applied=set())
    monkeypatch.setattr(migrate, "get_connection", lambda: conn)

    applied = migrate.apply_migrations(dry_run=True)

    assert applied == _names()                      # reports what it *would* do …
    # … but writes nothing: no migration bodies, no ledger inserts
    for path in migrate.discover_migrations():
        assert path.read_text(encoding="utf-8") not in _executed_sql(conn)
    assert not any("INSERT INTO schema_migrations" in s for s in _executed_sql(conn))


def test_failure_rolls_back_and_reraises(monkeypatch):
    conn = FakeConn(applied=set())
    monkeypatch.setattr(migrate, "get_connection", lambda: conn)
    # Make the first *migration body* execution fail (after the lock/ledger setup).
    orig_execute = FakeCursor.execute

    def flaky_execute(self, sql, params=None):
        if sql.strip().startswith("--") or "CREATE TABLE IF NOT EXISTS users" in sql:
            raise RuntimeError("db exploded")
        return orig_execute(self, sql, params)

    monkeypatch.setattr(FakeCursor, "execute", flaky_execute)
    try:
        migrate.apply_migrations()
        raise AssertionError("expected the migration failure to propagate")
    except RuntimeError:
        pass
    assert conn.rolledback is True
    assert conn.closed is True


def test_list_cli_does_not_touch_db(capsys, monkeypatch):
    def no_db():
        raise AssertionError("--list must not open a DB connection")

    monkeypatch.setattr(migrate, "get_connection", no_db)
    rc = migrate._main(["--list"])
    assert rc == 0
    assert "001_web_ui.sql" in capsys.readouterr().out
