import asyncio
import json
import math
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import auth
import conversations as convo
import github_issues
import job_tickets
import migrate
import ris_webhooks
import run_issue_github
import run_issues
from auth import AdminUser, CurrentUser
from database import list_users, set_notify_prefs, set_user_role, upsert_user
from version import get_version

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


app = FastAPI(title="TWAIN API", version=get_version(), lifespan=lifespan)

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


def git_sha() -> str:
    """The commit this image was built from, per api/Dockerfile's build arg.

    "unknown" when the API runs from a checkout rather than a built image, which is
    the honest answer for local dev and is distinguishable from a real SHA.

    Read per call rather than captured at import so a test can set the variable
    without reloading this module -- reloading it would build a second FastAPI app
    while other test modules still hold a reference to the first.
    """
    return os.getenv("TWAIN_GIT_SHA", "unknown")


@app.get("/api/health")
async def health_check():
    """Health check, and which commit is serving it.

    The SHA is here because "did the API deploy?" had no answer: this endpoint
    returned a bare {"status": "ok"} from every version, ``FastAPI(version=...)``
    is hardcoded, and CloudFront serves the SPA for /openapi.json -- so the only
    way to tell one release from another was to authenticate and probe behaviour.
    A deploy is now verifiable with one unauthenticated curl.
    """
    return {"status": "ok", "commit": git_sha(), "version": get_version()}


@app.get("/api/version")
def version_info():
    """The API's release version (YYYY.MM.DD.NNN) and commit; no credentials needed."""
    return {"service": "twain-api", "version": get_version(), "commit": git_sha()}


@app.post("/api/job-tickets/urls")
def job_ticket_urls(body: dict, x_twain_ticket: Annotated[str | None, Header()] = None):
    """Presigned S3 URLs for a Slurm job, traded for its job ticket (see job_tickets.py).

    No user auth: the job on RIS has no user; the ticket -- random, scoped to
    one run attempt, expiring -- is the credential, sent as X-TWAIN-Ticket.
    """
    try:
        return job_tickets.handle(x_twain_ticket or "", body)
    except job_tickets.TicketError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


#: ris-api events are a few hundred bytes; refuse anything far larger unread.
RIS_WEBHOOK_MAX_BYTES = 64 * 1024


@app.post("/api/ris/webhooks", status_code=204)
async def ris_webhook(request: Request):
    """Receive a signed RIS API job event (see ris_webhooks.py).

    No user auth: the Standard Webhooks signature is the authentication. A
    duplicate delivery is a 204 like a new one -- ris-api delivers at least
    once and only needs to hear that it can stop.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > RIS_WEBHOOK_MAX_BYTES:
        raise HTTPException(status_code=413, detail="webhook body too large")
    body = await request.body()
    if len(body) > RIS_WEBHOOK_MAX_BYTES:
        raise HTTPException(status_code=413, detail="webhook body too large")
    try:
        await run_in_threadpool(ris_webhooks.handle, request.headers, body)
    except ris_webhooks.WebhookError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return Response(status_code=204)


@app.get("/api/libraries")
def list_libraries(user: CurrentUser):
    """What TWAIN knows about, and which of it this cluster can actually run.

    Served from the snapshot the runner publishes (``library_availability``): the
    API cannot probe the cluster envs itself -- it is a separate deployable with no
    access to that filesystem -- so the runner, which lives with the envs, records
    the verdicts and this reads them. Requires a signed-in user like every other
    route; the list describes the deployment, not public information.
    """
    rows = convo.list_library_availability()
    return {
        "data": {
            "libraries": rows,
            "installed": sum(1 for row in rows if row.get("installed")),
            "total": len(rows),
            # Newest probe wins: the app shows how fresh the answer is, so a runner
            # that has not restarted since a provision run is visible as such.
            "checked_at": max(
                (row["checked_at"] for row in rows if row.get("checked_at")),
                default=None,
            ),
        }
    }


# ── Interim email login (pre-SSO) ─────────────────────────────────────────────
class InterimLogin(BaseModel):
    email: str


@app.post("/api/auth/login")
def interim_login(body: InterimLogin):
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
def get_me(user: CurrentUser):
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
def put_notify_prefs(body: NotifyPrefs, user: CurrentUser):
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
def admin_list_users(_admin: AdminUser):
    """List all users. Admin only."""
    users = list_users()
    return {"data": users, "count": len(users)}


@app.patch("/api/admin/users/{user_id}/role")
def admin_set_user_role(user_id: str, body: RoleUpdate, _admin: AdminUser):
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
def create_issue(body: CreateIssue, user: CurrentUser):
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
    # The bar the result is judged against at VALIDATE: a list of
    # {metric_name, target_value, tolerance}. Editable because TWAIN often has no
    # defensible target and writes null, which leaves the answer with nothing to be
    # checked against; only the researcher knows what "close enough" is. Null
    # target/tolerance are preserved, so clearing a bar is expressible too.
    acceptance_metrics: list[dict] | None = None


class RerunConversation(BaseModel):
    # Pipeline stage to restart from (e.g. "CLARIFY"). Validated against
    # convo.RERUNNABLE_STATES in the handler.
    state: str
    # Optional mid-session revision: the researcher's "here's what to change"
    # message, folded into the run's intent before re-planning.
    feedback: str | None = None
    # Replacement opening request, for re-running from INTAKE. Only INTAKE
    # re-reads the raw request (every later stage works from the IntentSpec it
    # produced), so the handler rejects it for any other target rather than
    # accepting an edit that would silently do nothing.
    request: str | None = None
    # Replacement resource request (cpu_count, gpu_count, ram GB, max_time hours),
    # for re-running the SAME plan with different resources. Only accepted for
    # targets after PLAN: re-running PLAN itself synthesizes a fresh plan, which
    # would overwrite the patch -- accepting it there would look like it worked and
    # silently do nothing, the same trap `request` avoids for later stages.
    slurm_request: dict | None = None
    # Replacement acceptance criteria, same shape and same after-PLAN restriction as
    # slurm_request: the plan they patch has to survive the rewind.
    acceptance_metrics: list[dict] | None = None


def _require_own_conversation(conversation_id: str, user: dict) -> dict:
    conversation = convo.get_conversation(conversation_id, user["id"])
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conversation


@app.post("/api/conversations")
def start_conversation(body: CreateConversation, user: CurrentUser):
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
def list_my_conversations(user: CurrentUser):
    """List the caller's conversations, newest first."""
    items = convo.list_conversations(user["id"])
    return {"data": items, "count": len(items)}


@app.get("/api/conversations/{conversation_id}")
def get_conversation_detail(conversation_id: str, user: CurrentUser):
    """Return a conversation plus its full transcript."""
    conversation = _require_own_conversation(conversation_id, user)
    return {"data": {**conversation, "messages": convo.list_messages(conversation_id)}}


@app.delete("/api/conversations/{conversation_id}")
def remove_conversation(conversation_id: str, user: CurrentUser):
    """Delete a conversation and all of its data (owner only)."""
    if not convo.delete_conversation(conversation_id, user["id"]):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"data": {"id": conversation_id, "deleted": True}}


@app.post("/api/conversations/{conversation_id}/messages")
def post_message(conversation_id: str, body: SendMessage, user: CurrentUser):
    """Add a user turn (a chat reply or an answer to a clarification question)."""
    _require_own_conversation(conversation_id, user)
    if not body.content.strip():
        raise HTTPException(status_code=422, detail="content must not be empty.")
    return {"data": convo.add_message(conversation_id, body.content)}


@app.post("/api/conversations/{conversation_id}/approval")
def post_approval(conversation_id: str, body: SendApproval, user: CurrentUser):
    """Answer a plan-approval gate ('approve' resumes the run, 'reject' stops it)."""
    _require_own_conversation(conversation_id, user)
    return {
        "data": convo.add_approval_response(
            conversation_id, body.decision, slurm_request=body.slurm_request,
            acceptance_metrics=body.acceptance_metrics,
        )
    }


@app.post("/api/conversations/{conversation_id}/terminate")
def post_terminate(conversation_id: str, user: CurrentUser):
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
def rerun_conversation(conversation_id: str, body: RerunConversation, user: CurrentUser):
    """Re-run a conversation from an earlier pipeline stage.

    Resets that stage and everything after it and drives the run again; the
    stages before it are kept as input. Allowed on a finished run, and on one
    suspended at a gate (nothing is driving a suspended run) so the researcher
    can redirect it from the accept-or-rerun question instead of only being
    offered the automatic correction loop.

    ``request`` replaces the opening prompt and is only accepted with INTAKE,
    the one stage that re-reads it.
    """
    _require_own_conversation(conversation_id, user)
    state = body.state.strip().upper()
    if state not in convo.RERUNNABLE_STATES:
        raise HTTPException(
            status_code=422,
            detail=f"state must be one of: {', '.join(convo.RERUNNABLE_STATES)}",
        )
    request = (body.request or "").strip() or None
    if request and state != "INTAKE":
        raise HTTPException(
            status_code=422,
            detail="An edited request only applies when re-running from INTAKE; "
                   "every later stage works from the spec intake already produced.",
        )
    # Stages that run AFTER plan synthesis, i.e. the ones where the plan on disk
    # survives the rewind and can therefore be patched.
    after_plan = convo.RERUNNABLE_STATES[convo.RERUNNABLE_STATES.index("PLAN") + 1:]
    slurm_request = body.slurm_request or None
    acceptance_metrics = body.acceptance_metrics or None
    if (slurm_request or acceptance_metrics) and state not in after_plan:
        raise HTTPException(
            status_code=422,
            detail="Edited resources and acceptance criteria only apply when "
                   f"re-running from a stage after PLAN ({', '.join(after_plan)}); "
                   "re-running PLAN itself synthesizes a new plan, which would "
                   "discard them.",
        )
    try:
        conversation = convo.rerun_conversation(
            conversation_id, user["id"], state,
            feedback=(body.feedback or "").strip() or None,
            request=request,
            slurm_request=slurm_request,
            acceptance_metrics=acceptance_metrics,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"data": conversation}


async def _sse_event_stream(conversation_id: str):
    """Yield run_events as Server-Sent Events until the run reaches a terminal state.

    Async on purpose: a sync generator here is iterated in the threadpool, and
    its ``time.sleep`` between ticks would hold a worker thread for the whole
    life of the stream -- enough open streams and no request could get one.
    Sleeping on the event loop costs nothing; only the two DB reads per tick
    borrow a thread.
    """
    last_id = 0
    deadline = time.monotonic() + SSE_MAX_SECONDS
    while True:
        for event in await run_in_threadpool(convo.get_events, conversation_id, last_id):
            last_id = event["id"]
            yield f"event: {event['event_type']}\ndata: {json.dumps(event, default=str)}\n\n"
        conversation = await run_in_threadpool(convo.get_conversation_status, conversation_id)
        if conversation is None or conversation in convo.TERMINAL_STATUSES:
            yield f"event: done\ndata: {json.dumps({'status': conversation})}\n\n"
            return
        if time.monotonic() >= deadline:
            yield 'event: done\ndata: {"status": "timeout"}\n\n'
            return
        await asyncio.sleep(SSE_POLL_SECONDS)


#: Event types the live activity feed serves (stage checklists + job log).
ACTIVITY_EVENT_TYPES = ("stage.progress", "job.log", "run.error")
ACTIVITY_PAGE_LIMIT = 500


@app.get("/api/conversations/{conversation_id}/activity")
async def conversation_activity(conversation_id: str, user: CurrentUser, after: int = 0):
    """In-stage progress + job log since event ``after`` (an id cursor).

    The chat screen's live checklist polls this rather than the SSE stream:
    EventSource can't send the bearer token, so under auth the stream is
    refused while this rides the normal authenticated client. Returns
    ``{"data": [...events], "next_after": <cursor>}``; pass ``next_after`` back
    to get only what's new.
    """
    if not await run_in_threadpool(convo.owns_conversation, conversation_id, user["id"]):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    events = await run_in_threadpool(
        convo.get_activity, conversation_id, max(0, after),
        ACTIVITY_EVENT_TYPES, ACTIVITY_PAGE_LIMIT)
    return {"data": events, "next_after": events[-1]["id"] if events else max(0, after)}


@app.get("/api/conversations/{conversation_id}/run-files")
def conversation_run_files(conversation_id: str, user: CurrentUser, attempt: int | None = None):
    """Short-lived download links to a cluster attempt's bundle and outputs.

    Owner-only. The failure card uses these to reproduce a cluster failure: the
    files live in S3 (the node's scratch is gone, the worker's path was never
    reachable). ``attempt`` defaults to the newest. 404 when the run never
    reached the cluster.
    """
    if not convo.owns_conversation(conversation_id, user["id"]):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    found = convo.cluster_attempt(conversation_id, attempt)
    if not found or not found.get("s3_prefix"):
        raise HTTPException(status_code=404, detail="This run has no cluster files.")
    try:
        files = job_tickets.run_file_urls(found["s3_prefix"])
    except job_tickets.TicketError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"job_id": found["job_id"], "attempt": found["attempt"], **files}


@app.get("/api/conversations/{conversation_id}/stream")
async def stream_conversation(conversation_id: str, user: CurrentUser):
    """Live Server-Sent Events of pipeline progress for a conversation."""
    await run_in_threadpool(_require_own_conversation, conversation_id, user)
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

    Convention: the run's ``main.py`` prints a JSON object with the computed
    property, e.g. ``{"property": "band_gap", "band_gap": 6.73, ...}``. Returns
    the last such object found, or None.

    The object may span several lines. Scripts routinely print it with
    ``json.dumps(obj, indent=2)``, and an earlier version of this function tested
    ``line.startswith("{") and line.endswith("}")`` -- which only ever matches a
    one-line object, so a pretty-printed result parsed to None and the report card
    fell back to "no structured result found" while displaying that very JSON as
    raw output. The value was never lost (INTERPRET parses the same stdout for the
    normalized metric), but the headline was blank.

    Decoding starts only at a line that begins with ``{``, so a brace inside an
    engine's log text cannot start a spurious parse, and the scan stays cheap on
    the tens of kilobytes of solver output these runs produce.
    """
    if not isinstance(execution_result, dict):
        return None
    stdout = execution_result.get("stdout")
    if not isinstance(stdout, str):
        return None
    decoder = json.JSONDecoder()
    result = None
    consumed = 0
    # Advance past each object decoded. Without that, a top-level object holding a
    # LIST of dicts gets re-decoded once per element -- an EOS scan with 200 points
    # cost 201 decodes -- and the last inner element wins over the outer object
    # that actually carries "property".
    for match in re.finditer(r"^[ \t]*\{", stdout, re.M):
        start = match.end() - 1
        if start < consumed:
            continue
        try:
            parsed, consumed = decoder.raw_decode(stdout, start)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            result = parsed
    # json.loads accepts bare NaN; the response renderer does not.
    return _json_safe(result)


@app.get("/api/conversations/{conversation_id}/report")
def get_report(conversation_id: str, user: CurrentUser):
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


# ── Report an issue from the run window ───────────────────────────────────────
class SubmitRunIssue(BaseModel):
    category: Literal["bug", "library", "result", "other"] = "other"
    title: str
    description: str


@app.get("/api/conversations/{conversation_id}/issue-context")
def get_issue_context(conversation_id: str, user: CurrentUser):
    """Preview exactly what would be attached to an issue filed against this run.

    Submitting publishes the run's data to the issue tracker, so the run window
    shows this first — the user consents to a snapshot they can actually see,
    not to a description of one. Also reports whether GitHub is configured, so
    the form can say up front whether an issue will really be filed.
    """
    conversation = _require_own_conversation(conversation_id, user)
    return {
        "data": {
            "run_context": run_issues.collect_run_context(conversation),
            "github_configured": run_issue_github.issues_enabled(),
            "repo": run_issue_github.resolve_repo(),
            "categories": list(run_issue_github.CATEGORIES),
            "submitted": run_issues.list_issues(conversation_id),
        }
    }


@app.post("/api/conversations/{conversation_id}/issues")
def submit_run_issue(conversation_id: str, body: SubmitRunIssue, user: CurrentUser):
    """File a GitHub issue about this run, with the run's own data attached.

    The submission is recorded locally whether or not GitHub could be reached, so
    a report is never silently lost; the returned ``status`` distinguishes
    ``created`` from ``queued`` (no credentials configured) and ``failed``.
    """
    conversation = _require_own_conversation(conversation_id, user)
    title = run_issue_github.clean_title(body.title)
    description = body.description.strip()[: run_issue_github.MAX_DESCRIPTION]
    if not title:
        raise HTTPException(status_code=422, detail="title must not be empty.")
    if not description:
        raise HTTPException(status_code=422, detail="description must not be empty.")
    if run_issues.count_issues(conversation_id) >= run_issues.MAX_ISSUES_PER_RUN:
        raise HTTPException(
            status_code=429,
            detail=(
                f"This run already has {run_issues.MAX_ISSUES_PER_RUN} reported issues. "
                "Comment on an existing issue instead."
            ),
        )

    context = run_issues.collect_run_context(conversation)
    if run_issue_github.issues_enabled():
        result = run_issue_github.GitHubIssueClient().create_issue(
            title=title,
            body=run_issue_github.render_issue_body(
                context, category=body.category, description=description, reporter=user
            ),
            labels=run_issue_github.labels_for(body.category),
        )
    else:
        # Recorded, not filed: the deployment has no issue-tracker credentials.
        result = {
            "status": "queued", "issue_number": None, "issue_url": None,
            "error": "No GitHub credentials are configured for this deployment, so the "
                     "report was saved against the run but no issue was filed.",
        }
    recorded = run_issues.record_issue(
        conversation_id, user.get("id"), category=body.category, title=title,
        description=description, run_context=context, result=result,
    )
    return {"data": recorded}


@app.get("/api/conversations/{conversation_id}/issues")
def list_run_issues(conversation_id: str, user: CurrentUser):
    """Issues already reported against this run, newest first."""
    _require_own_conversation(conversation_id, user)
    items = run_issues.list_issues(conversation_id)
    return {"data": items, "count": len(items)}


@app.get("/api/conversations/{conversation_id}/artifacts")
def list_artifacts(conversation_id: str, user: CurrentUser):
    """List the artifacts (specs + generated code files) produced by the run."""
    _require_own_conversation(conversation_id, user)
    items = convo.list_artifacts(conversation_id)
    return {"data": items, "count": len(items)}


@app.get("/api/conversations/{conversation_id}/artifacts/{name:path}")
def get_artifact(conversation_id: str, name: str, user: CurrentUser):
    """Return one artifact's full content (name may contain '/', e.g. run_bundle/main.py)."""
    _require_own_conversation(conversation_id, user)
    artifact = convo.get_artifact(conversation_id, name)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return {"data": artifact}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)  # noqa: S104
