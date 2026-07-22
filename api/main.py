import json
import os
import time
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import conversations as convo
from auth import AdminUser, CurrentUser
from database import list_users, query_greetings, set_user_role

# How often (seconds) the SSE stream polls run_events, and its hard time cap.
SSE_POLL_SECONDS = float(os.getenv("SSE_POLL_SECONDS", "1.0"))
SSE_MAX_SECONDS = float(os.getenv("SSE_MAX_SECONDS", "1800"))

app = FastAPI(title="TWAIN API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8081",
        "http://localhost:3000",
        "http://localhost:3002",
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


@app.get("/api/greetings")
async def get_greetings():
    """Fetch all greetings from the database and return as JSON."""
    try:
        greetings = query_greetings()
        if not greetings:
            return JSONResponse(
                status_code=200,
                content={"data": [], "message": "No greetings found"},
            )
        return JSONResponse(
            status_code=200,
            content={
                "data": greetings,
                "count": len(greetings),
                "message": "Greetings retrieved successfully",
            },
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "message": "Failed to retrieve greetings"},
        )


class RoleUpdate(BaseModel):
    role: Literal["user", "admin"]


@app.get("/api/me")
async def get_me(user: CurrentUser):
    """Return the authenticated user (identity + role)."""
    return {"data": user}


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


# ── Conversations / chat (Phase 1) ────────────────────────────────────────────
class CreateConversation(BaseModel):
    request: str
    title: str | None = None
    # Where EXECUTE runs: 'slurm' submits to the RIS cluster, 'local' runs on
    # the runner host, None keeps the runner's env-configured default.
    compute_target: Literal["local", "slurm"] | None = None


class SendMessage(BaseModel):
    content: str


class SendApproval(BaseModel):
    decision: Literal["approve", "reject"]
    # Optional plan-unit overrides (ram GB, max_time hours) from the approval card.
    slurm_request: dict | None = None


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
    conversation = convo.create_conversation(
        user["id"], body.request, body.title, compute_target=body.compute_target
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


def _load_json_artifact(conversation_id: str, name: str):
    """Fetch an artifact and parse it as JSON, or return raw text / None."""
    artifact = convo.get_artifact(conversation_id, name)
    if artifact is None:
        return None
    try:
        return json.loads(artifact["content"])
    except (ValueError, TypeError):
        return artifact["content"]


@app.get("/api/conversations/{conversation_id}/report")
async def get_report(conversation_id: str, user: CurrentUser):
    """Assemble a run report: summary + the list of downloadable artifacts."""
    conversation = _require_own_conversation(conversation_id, user)
    return {
        "data": {
            "conversation": conversation,
            "final_state": conversation["current_state"],
            "status": conversation["status"],
            "plan": _load_json_artifact(conversation_id, "execution_plan"),
            "execution_result": _load_json_artifact(conversation_id, "execution_result"),
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
