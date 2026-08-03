import json
import math
import os
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import auth
import conversations as convo
import github_issues
import migrate
from auth import AdminUser, CurrentUser
from database import list_users, set_notify_prefs, set_user_role, upsert_user

# How often (seconds) the SSE stream polls run_events, and its hard time cap.
SSE_POLL_SECONDS = float(os.getenv("SSE_POLL_SECONDS", "1.0"))
SSE_MAX_SECONDS = float(os.getenv("SSE_MAX_SECONDS", "1800"))


def _flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Apply idempotent DB migrations on boot so a deploy needs no manual step.

    Disable with ``RUN_MIGRATIONS_ON_STARTUP=false`` (e.g. when migrations run as
    a separate one-off task). A failure here intentionally stops the API from
    serving on an unmigrated schema — the right signal for a bad deploy — rather
    than coming up "healthy" but broken.
    """
    if _flag("RUN_MIGRATIONS_ON_STARTUP", default=True):
        try:
            applied = migrate.apply_migrations()
            print(
                f"[startup] migrations applied: {', '.join(applied)}"
                if applied else "[startup] database schema already up to date"
            )
        except Exception as exc:  # noqa: BLE001 - surface loudly, then fail fast
            print(f"[startup] FATAL: database migration failed: {exc}")
            raise
    yield


app = FastAPI(title="TWAIN API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",  # app/package.json "web" script pins this port
        "http://localhost:3002",
        "http://localhost:8081",  # default `npx expo start --web` port
        "https://d1z5umg4xc2bl8.cloudfront.net",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


# ── Interim email login (pre-SSO) ─────────────────────────────────────────────
class InterimLogin(BaseModel):
    email: str


@app.post("/api/auth/login")
async def interim_login(body: InterimLogin):
    """Interim email sign-in: validate the email, upsert the user, mint a token.

    Available only when ``INTERIM_JWT_SECRET`` is configured. Entra tokens are
    still accepted directly on every other endpoint once SSO is wired up.
    """
    if not auth.interim_auth_available():
        raise HTTPException(status_code=503, detail="Interim auth is not configured.")
    email = body.email.strip().lower()
    if not auth.email_allowed(email):
        raise HTTPException(
            status_code=403, detail="This email is not permitted to sign in."
        )
    user = upsert_user(
        f"interim:{email}",
        email,
        email,
        bootstrap_admin=email in auth.BOOTSTRAP_ADMIN_EMAILS,
    )
    return {"data": {"token": auth.mint_interim_token(user), "user": user}}


class RoleUpdate(BaseModel):
    role: Literal["user", "admin"]


@app.get("/api/me")
async def get_me(user: CurrentUser):
    """Return the authenticated user (identity + role + notification prefs)."""
    return {"data": user}


# Keep in lockstep with runner/notifications.py NOTIFY_KINDS (the reasons the
# runner actually emails about). The api and runner are separate deployables,
# so the list is mirrored here rather than imported.
NOTIFY_KINDS = ("input", "approval", "completed", "failed", "terminated")


class NotifyPrefs(BaseModel):
    """The Settings page sends its whole state; missing key = "send"."""

    enabled: bool = True
    kinds: dict[str, bool] = {}


@app.put("/api/me/notifications")
async def put_notify_prefs(body: NotifyPrefs, user: CurrentUser):
    """Replace the caller's email notification preferences.

    The runner consults these before every send: emails off entirely
    (``enabled=false``) or per-kind opt-outs (``kinds[kind]=false``).
    """
    unknown = sorted(set(body.kinds) - set(NOTIFY_KINDS))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown notification kind(s): {', '.join(unknown)}; "
                   f"valid kinds: {', '.join(NOTIFY_KINDS)}",
        )
    prefs = set_notify_prefs(user["id"], body.model_dump())
    if prefs is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return {"data": prefs}


@app.get("/api/admin/users")
async def admin_list_users(_admin: AdminUser):
    """List all users. Admin only."""
    users = list_users()
    return {"data": users, "count": len(users)}


@app.patch("/api/admin/users/{user_id}/role")
async def admin_set_user_role(user_id: str, body: RoleUpdate, _admin: AdminUser):
    """Change a user's role. Admin only."""
    updated = set_user_role(user_id, body.role)
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return {"data": updated}


# ── GitHub issue submission ───────────────────────────────────────────────────
class CreateIssue(BaseModel):
    title: str
    body: str = ""


@app.post("/api/issues", status_code=201)
async def create_issue(body: CreateIssue, user: CurrentUser):
    """Open a GitHub issue on the TWAIN repo for the signed-in user.

    Issues are created by a single service PAT, so the caller's email — taken
    from their validated token, not the request body — is embedded in the issue
    for attribution.
    """
    if not body.title.strip():
        raise HTTPException(status_code=422, detail="title must not be empty.")
    try:
        result = github_issues.create_issue(
            title=body.title,
            body=body.body,
            email=user.get("email", ""),
            name=user.get("name"),
        )
    except github_issues.GitHubError as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not create GitHub issue: {exc}"
        ) from exc
    return {"data": result}


# ── Conversations / chat (Phase 1) ────────────────────────────────────────────
class CreateConversation(BaseModel):
    request: str
    title: str | None = None
    # Optional per-run LLM cost cap (USD). None => deployment default (the runner
    # falls back to TWAIN_RUN_MAX_COST). Must be positive when supplied.
    max_cost: float | None = None


class SendMessage(BaseModel):
    content: str


class SendApproval(BaseModel):
    decision: Literal["approve", "reject"]
    # Optional plan-unit overrides (ram GB, max_time hours) from the approval card.
    slurm_request: dict | None = None


class RerunConversation(BaseModel):
    # Pipeline stage to restart from (e.g. "CLARIFY"). Validated against
    # convo.RERUNNABLE_STATES in the handler.
    state: str
    # Optional mid-session revision: the researcher's "here's what to change"
    # message, folded into the run's intent before re-planning.
    feedback: str | None = None


def _require_own_conversation(conversation_id: str, user: dict) -> dict:
    conversation = convo.get_conversation(conversation_id, user["id"])
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation


@app.post("/api/conversations")
async def start_conversation(body: CreateConversation, user: CurrentUser):
    """Start a new run from a natural-language request and enqueue it."""
    if not body.request.strip():
        raise HTTPException(status_code=422, detail="request must not be empty.")
    if body.max_cost is not None and body.max_cost <= 0:
        raise HTTPException(status_code=422, detail="max_cost must be a positive number.")
    conversation = convo.create_conversation(
        user["id"], body.request, body.title, max_cost=body.max_cost
    )
    return {"data": conversation}


@app.get("/api/conversations")
async def list_my_conversations(user: CurrentUser):
    """List the caller's conversations, newest first."""
    items = convo.list_conversations(user["id"])
    return {"data": items, "count": len(items)}


@app.get("/api/conversations/{conversation_id}")
async def get_conversation_detail(conversation_id: str, user: CurrentUser):
    """Return a conversation plus its full transcript."""
    conversation = _require_own_conversation(conversation_id, user)
    return {"data": {**conversation, "messages": convo.list_messages(conversation_id)}}


@app.delete("/api/conversations/{conversation_id}")
async def remove_conversation(conversation_id: str, user: CurrentUser):
    """Delete a conversation and all of its data (owner only)."""
    if not convo.delete_conversation(conversation_id, user["id"]):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"data": {"id": conversation_id, "deleted": True}}


@app.post("/api/conversations/{conversation_id}/messages")
async def post_message(conversation_id: str, body: SendMessage, user: CurrentUser):
    """Add a user turn (a chat reply or an answer to a clarification question)."""
    _require_own_conversation(conversation_id, user)
    if not body.content.strip():
        raise HTTPException(status_code=422, detail="content must not be empty.")
    return {"data": convo.add_message(conversation_id, body.content)}


@app.post("/api/conversations/{conversation_id}/approval")
async def post_approval(conversation_id: str, body: SendApproval, user: CurrentUser):
    """Answer a plan-approval gate ('approve' resumes the run, 'reject' stops it)."""
    _require_own_conversation(conversation_id, user)
    return {
        "data": convo.add_approval_response(
            conversation_id, body.decision, slurm_request=body.slurm_request
        )
    }


@app.post("/api/conversations/{conversation_id}/terminate")
async def post_terminate(conversation_id: str, user: CurrentUser):
    """Ask the runner to stop this run at the next opportunity.

    Records a 'terminate' control message and flips the conversation to
    'cancelling'; the runner notices between stages / polls, cancels any
    in-flight Slurm job, and settles the conversation as 'cancelled'.
    """
    conversation = _require_own_conversation(conversation_id, user)
    if conversation["status"] in convo.TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="Run already finished.")
    return {"data": convo.request_termination(conversation_id)}


@app.post("/api/conversations/{conversation_id}/rerun")
async def rerun_conversation(conversation_id: str, body: RerunConversation, user: CurrentUser):
    """Re-run a finished conversation from an earlier pipeline stage.

    Resets that stage and everything after it and drives the run again; the
    stages before it are kept as input. Only allowed on a finished run.
    """
    _require_own_conversation(conversation_id, user)
    state = body.state.strip().upper()
    if state not in convo.RERUNNABLE_STATES:
        raise HTTPException(
            status_code=422,
            detail=f"state must be one of: {', '.join(convo.RERUNNABLE_STATES)}",
        )
    try:
        conversation = convo.rerun_conversation(
            conversation_id, user["id"], state,
            feedback=(body.feedback or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"data": conversation}


def _sse_event_stream(conversation_id: str):
    """Yield run_events as Server-Sent Events until the run reaches a terminal state."""
    last_id = 0
    deadline = time.monotonic() + SSE_MAX_SECONDS
    while True:
        for event in convo.get_events(conversation_id, last_id):
            last_id = event["id"]
            yield f"event: {event['event_type']}\ndata: {json.dumps(event, default=str)}\n\n"
        conversation = convo.get_conversation_status(conversation_id)
        if conversation is None or conversation in convo.TERMINAL_STATUSES:
            yield f"event: done\ndata: {json.dumps({'status': conversation})}\n\n"
            return
        if time.monotonic() >= deadline:
            yield 'event: done\ndata: {"status": "timeout"}\n\n'
            return
        time.sleep(SSE_POLL_SECONDS)


@app.get("/api/conversations/{conversation_id}/stream")
async def stream_conversation(conversation_id: str, user: CurrentUser):
    """Live Server-Sent Events of pipeline progress for a conversation."""
    _require_own_conversation(conversation_id, user)
    return StreamingResponse(
        _sse_event_stream(conversation_id), media_type="text/event-stream"
    )


def _json_safe(value):
    """Replace non-finite floats with None so the response can be serialized.

    ``json.loads`` accepts the bare ``NaN``/``Infinity`` that a generated script
    prints and that older artifacts still contain, but Starlette renders
    responses with ``allow_nan=False``. Letting one through does not degrade a
    field -- it raises during rendering and turns the whole report into a 500.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _load_json_artifact(conversation_id: str, name: str):
    """Fetch an artifact and parse it as JSON, or return raw text / None."""
    artifact = convo.get_artifact(conversation_id, name)
    if artifact is None:
        return None
    try:
        return _json_safe(json.loads(artifact["content"]))
    except (ValueError, TypeError):
        return artifact["content"]


def _extract_result(execution_result):
    """Pull the structured result the generated script prints to stdout, if any.

    Convention: the run's ``main.py`` prints a single-line JSON object with the
    computed property, e.g. ``{"property": "band_gap", "band_gap": 6.73, ...}``.
    Returns the last such object found, or None.
    """
    if not isinstance(execution_result, dict):
        return None
    stdout = execution_result.get("stdout")
    if not isinstance(stdout, str):
        return None
    result = None
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                result = parsed
    # json.loads accepts bare NaN; the response renderer does not.
    return _json_safe(result)


@app.get("/api/conversations/{conversation_id}/report")
async def get_report(conversation_id: str, user: CurrentUser):
    """Assemble a run report: headline result, summary, and downloadable artifacts."""
    conversation = _require_own_conversation(conversation_id, user)
    execution_result = _load_json_artifact(conversation_id, "execution_result")
    return {
        "data": {
            "conversation": conversation,
            "final_state": conversation["current_state"],
            "status": conversation["status"],
            "plan": _load_json_artifact(conversation_id, "execution_plan"),
            "execution_result": execution_result,
            "result": _extract_result(execution_result),
            "results_dir": (
                execution_result.get("artifacts_dir")
                if isinstance(execution_result, dict)
                else None
            ),
            # Epic 6 artifacts: the interpreted metric and the validation
            # verdict, so the report can say how the result was checked.
            "normalized_result": _load_json_artifact(conversation_id, "normalized_result"),
            "validation": _load_json_artifact(conversation_id, "validation_report"),
            "budget": _load_json_artifact(conversation_id, "budget"),
            "artifacts": convo.list_artifacts(conversation_id),
        }
    }


@app.get("/api/conversations/{conversation_id}/artifacts")
async def list_artifacts(conversation_id: str, user: CurrentUser):
    """List the artifacts (specs + generated code files) produced by the run."""
    _require_own_conversation(conversation_id, user)
    items = convo.list_artifacts(conversation_id)
    return {"data": items, "count": len(items)}


@app.get("/api/conversations/{conversation_id}/artifacts/{name:path}")
async def get_artifact(conversation_id: str, name: str, user: CurrentUser):
    """Return one artifact's full content (name may contain '/', e.g. run_bundle/main.py)."""
    _require_own_conversation(conversation_id, user)
    artifact = convo.get_artifact(conversation_id, name)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return {"data": artifact}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)  # noqa: S104
