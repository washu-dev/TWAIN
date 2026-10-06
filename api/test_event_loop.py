"""The API's event loop must never wait on the database (#165).

On 2026-10-06 one database connect hung (a Kerberos KDC lookup inside libpq
while the VPN was down). It ran on the event loop from an ``async def`` route,
so every request froze -- /api/health included -- and the app showed "timeout
of 10000ms exceeded" for everything. These tests pin the fix: blocking work
runs in the threadpool, so one slow call delays only its own request.
"""
import ast
import asyncio
import re
import time
from pathlib import Path

import httpx

import conversations
import database
import main
from auth import get_current_user

HERE = Path(__file__).parent
USER = {"id": "user-1", "subject": "s1", "email": "u@wustl.edu", "name": "U", "role": "user"}
SLOW_SECONDS = 1.5


def test_a_slow_database_call_does_not_stall_other_requests(monkeypatch):
    def slow_list(user_id):
        time.sleep(SLOW_SECONDS)   # a hung connect / slow query, as psycopg2 would
        return []
    monkeypatch.setattr(conversations, "list_conversations", slow_list)
    main.app.dependency_overrides[get_current_user] = lambda: USER
    try:
        async def scenario():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                start = time.monotonic()
                slow = asyncio.ensure_future(client.get("/api/conversations"))
                # Timed from when the slow request STARTED: if its DB call blocks
                # the loop, even this sleep can't resume until the call returns.
                await asyncio.sleep(0.1)
                health = await client.get("/api/health")
                health_took = time.monotonic() - start
                return health, health_took, await slow
        health, health_took, slow_response = asyncio.run(scenario())
    finally:
        main.app.dependency_overrides.clear()

    assert health.status_code == 200 and slow_response.status_code == 200
    # Before the fix /api/health waited out the whole slow call.
    assert health_took < SLOW_SECONDS / 3, f"/api/health took {health_took:.2f}s"


def test_slow_requests_overlap_instead_of_queueing(monkeypatch):
    def slow_list(user_id):
        time.sleep(SLOW_SECONDS)
        return []
    monkeypatch.setattr(conversations, "list_conversations", slow_list)
    main.app.dependency_overrides[get_current_user] = lambda: USER
    try:
        async def scenario():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                start = time.monotonic()
                await asyncio.gather(*(client.get("/api/conversations") for _ in range(3)))
                return time.monotonic() - start
        took = asyncio.run(scenario())
    finally:
        main.app.dependency_overrides.clear()
    assert took < 2 * SLOW_SECONDS, f"3 slow requests took {took:.2f}s -- serialized"


# ── static guard: no blocking work on the loop ─────────────────────────────────

def _async_defs_without_await(path: Path, *, routes_only: bool):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        is_route = any("app." in ast.unparse(d) for d in node.decorator_list)
        if routes_only and not is_route:
            continue
        if not re.search(r"\bawait\b", ast.unparse(node)):
            found.append(node.name)
    return found


def test_every_async_route_awaits_its_blocking_work():
    # A route that is `async def` yet never awaits runs entirely on the event
    # loop: any DB or HTTP call in it blocks every other request. Make it a
    # plain `def` (FastAPI runs it in the threadpool) or await run_in_threadpool.
    offenders = [n for n in _async_defs_without_await(HERE / "main.py", routes_only=True)
                 if n != "health_check"]   # no I/O at all
    assert offenders == []


def test_auth_dependency_is_sync():
    # It verifies the token (possibly fetching JWKS) and upserts the user on
    # every authenticated request -- that must happen off the event loop.
    assert _async_defs_without_await(HERE / "auth.py", routes_only=False) == []


# ── connection options ─────────────────────────────────────────────────────────

def test_connections_never_negotiate_gss_and_fail_fast(monkeypatch):
    monkeypatch.delenv("PGGSSENCMODE", raising=False)
    monkeypatch.delenv("DB_CONNECT_TIMEOUT", raising=False)
    assert database.connection_options() == {"connect_timeout": 10, "gssencmode": "disable"}

    monkeypatch.setenv("PGGSSENCMODE", "prefer")
    monkeypatch.setenv("DB_CONNECT_TIMEOUT", "3")
    assert database.connection_options() == {"connect_timeout": 3, "gssencmode": "prefer"}


def test_get_connection_passes_the_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(database, "_load_db_config", lambda: {"host": "h", "dbname": "d"})
    monkeypatch.setattr(database.psycopg2, "connect", lambda **kw: seen.update(kw) or "conn")
    monkeypatch.delenv("PGGSSENCMODE", raising=False)
    assert database.get_connection() == "conn"
    assert seen["gssencmode"] == "disable" and seen["connect_timeout"] == 10 and seen["host"] == "h"
