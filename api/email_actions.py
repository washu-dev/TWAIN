"""One-tap gate answers from email (the worker issues them: runner/email_actions.py).

``GET /api/actions/<token>`` shows a small confirmation page; its form's POST
answers the gate exactly as the app does (``add_approval_response`` for a plan,
``add_message`` for a yes/no question). There is no sign-in: the token is the
authority, like a password-reset link. That is why it is random, stored only as
SHA-256, bound to one gate message, single-use (every sibling button is spent
with it), short-lived, and refused once the run is no longer waiting on that
exact question -- answered in the app, re-planned, or terminated.

The same tokens answer a shared-environment change proposal (#187: approve /
reject, spent across every approver's email at once) and offer a finished run's
owner a re-run from EXECUTE once the change is live.

GET never acts. WashU mail is scanned by Microsoft Safe Links, which opens every
link in a message before the reader does; a GET that approved would let the
scanner approve runs.
"""
from __future__ import annotations

import hashlib
import html
import os
from dataclasses import dataclass

from psycopg2.extras import RealDictCursor

import conversations as convo
from database import get_connection

#: What each gate's choice means, in the words the confirmation page uses.
_VERB = {
    "approve": "approve the plan and start the calculation",
    "reject": "reject the plan (TWAIN will ask what to change)",
    "yes": "run the heavy calculation now",
    "no": "not run the heavy calculation now",
    "accept": "accept the result as it stands",
    "rerun": "spend another correction pass on the result",
    "EXECUTE": "re-run the calculation from EXECUTE",
}
_PROPOSAL_VERB = {
    "approve": "approve this shared-environment change (TWAIN builds, verifies, then switches)",
    "reject": "reject this shared-environment change (nothing on RIS is touched)",
}

#: Security headers for every page this module serves.
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
}


@dataclass
class Action:
    state: str                    # ok | used | expired | answered | unknown
    session_id: str | None = None
    gate_message_id: int | None = None
    gate_kind: str | None = None
    choice: str | None = None
    label: str | None = None
    title: str | None = None
    proposal_id: int | None = None
    proposal: str | None = None   # "add <package> to <env>", for the pages
    recipient: str | None = None
    user_id: str | None = None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_LOOKUP = """
    SELECT a.session_id::text AS session_id, a.gate_message_id, a.gate_kind, a.choice, a.label,
           a.used_at, a.expires_at < now() AS expired, c.title, c.user_id::text AS user_id,
           a.proposal_id, a.recipient,
           CASE WHEN p.id IS NULL THEN NULL
                ELSE 'add ' || p.package || ' to the shared ' || p.env || ' environment' END
               AS proposal,
           CASE a.gate_kind
               WHEN 'env_change' THEN p.status = 'pending'
               WHEN 'rerun' THEN c.status IN ('completed', 'error', 'rejected', 'cancelled')
               ELSE c.status IN ('awaiting_approval', 'awaiting_input') AND a.gate_message_id = (
                   SELECT m.id FROM messages m
                   WHERE m.conversation_id = a.session_id
                     AND m.kind IN ('clarification', 'heavy_confirm', 'validation_gate',
                                    'revision_request', 'approval_request')
                   ORDER BY m.id DESC LIMIT 1)
           END AS pending
    FROM email_actions a
    LEFT JOIN conversations c ON c.id = a.session_id
    LEFT JOIN env_proposals p ON p.id = a.proposal_id
    WHERE a.token_hash = %s
"""


def _state(row) -> Action:
    if row is None:
        return Action("unknown")
    state = ("used" if row["used_at"] is not None else "expired" if row["expired"]
             else "ok" if row["pending"] else "answered")
    return Action(state, row["session_id"], row["gate_message_id"], row["gate_kind"],
                  row["choice"], row["label"], row["title"], row["proposal_id"],
                  row["proposal"], row["recipient"], row["user_id"])


def peek(token: str) -> Action:
    """What ``token`` would do, without doing it."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(_LOOKUP + ";", (token_hash(token),))
        return _state(cursor.fetchone())
    finally:
        conn.close()


def consume(token: str) -> Action:
    """Spend ``token`` (and its siblings) and answer the gate; returns what happened."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(_LOOKUP + " FOR UPDATE OF a;", (token_hash(token),))
        action = _state(cursor.fetchone())
        if action.state != "ok":
            conn.rollback()
            return action
        if action.gate_kind == "env_change":
            # Every approver's buttons are spent together: one decision per proposal.
            cursor.execute("UPDATE email_actions SET used_at = now() "
                           "WHERE proposal_id = %s AND used_at IS NULL;", (action.proposal_id,))
            cursor.execute(
                "UPDATE env_proposals SET status = %s, decided_by = %s, decided_at = now() "
                "WHERE id = %s AND status = 'pending';",
                ("approved" if action.choice == "approve" else "rejected",
                 action.recipient, action.proposal_id))
        elif action.gate_kind == "rerun":
            cursor.execute("UPDATE email_actions SET used_at = now() WHERE session_id = %s "
                           "AND gate_kind = 'rerun' AND used_at IS NULL;", (action.session_id,))
        else:
            cursor.execute(
                "UPDATE email_actions SET used_at = now() "
                "WHERE session_id = %s AND gate_message_id = %s AND used_at IS NULL;",
                (action.session_id, action.gate_message_id))
        conn.commit()
    finally:
        conn.close()
    if action.gate_kind == "env_change":
        return action          # the cluster monitor picks the approved proposal up
    if action.gate_kind == "rerun":
        try:
            convo.rerun_conversation(action.session_id, action.user_id, action.choice)
        except ValueError:     # it was re-run (or restarted) in the app meanwhile
            action.state = "answered"
        return action
    if action.gate_kind == "approval_request":
        convo.add_approval_response(action.session_id, action.choice)
    else:
        convo.add_message(action.session_id, action.choice)
    return action


# ------------------------------------------------------------------ pages
def _app_url() -> str:
    return os.getenv("TWAIN_APP_URL", "https://d1z5umg4xc2bl8.cloudfront.net").rstrip("/")


def _page(title: str, body: str, session_id: str | None = None) -> str:
    link = (f'<p style="margin-top:24px"><a style="color:#BA0C2F" '
            f'href="{html.escape(_app_url())}/conversations/{html.escape(session_id)}">'
            f'Open the run in TWAIN</a></p>') if session_id else ""
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)} - TWAIN</title></head>"
            "<body style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:520px;"
            "margin:48px auto;padding:0 20px;color:#1A1A1A'>"
            "<div style='font-weight:700;color:#BA0C2F;letter-spacing:1px'>TWAIN</div>"
            f"<h1 style='font-size:20px'>{html.escape(title)}</h1>{body}{link}</body></html>")


def confirm_page(action: Action) -> str:
    if action.state != "ok":
        return result_page(action)
    if action.gate_kind == "env_change":
        run = html.escape(action.proposal or "a shared-environment change")
        verb = html.escape(_PROPOSAL_VERB.get(action.choice or "", action.choice or ""))
    else:
        run = html.escape(action.title or "your run")
        verb = html.escape(_VERB.get(action.choice or "", action.choice or ""))
    body = (f"<p>For <strong>{run}</strong>, you're about to <strong>{verb}</strong>.</p>"
            "<form method='post'><button type='submit' style='background:#BA0C2F;color:#fff;"
            "border:0;border-radius:6px;padding:12px 22px;font-size:15px;font-weight:600;"
            f"cursor:pointer'>Confirm: {html.escape(action.label or 'Confirm')}</button></form>"
            "<p style='color:#5A5A5A;font-size:13px'>Changed your mind, or want to edit the "
            "resources first? Open the run instead.</p>")
    return _page(action.label or "Confirm", body, action.session_id)


def result_page(action: Action, *, done: bool = False) -> str:
    if done and action.gate_kind == "env_change":
        text = ("Approved. TWAIN will build the new version beside the live one, verify it, "
                "and only then switch to it; you'll get an email with the outcome."
                if action.choice == "approve" else
                "Rejected. Nothing on RIS will be changed.")
        return _page("Done", f"<p>{html.escape(text)}</p>", action.session_id)
    if done and action.gate_kind == "rerun":
        return _page("Done", "<p>The run is starting again from EXECUTE. You'll get an email "
                     "when it needs you again or finishes.</p>", action.session_id)
    if done:
        return _page("Done", "<p>Thanks: TWAIN has your answer and the run continues. "
                     "You'll get an email when it needs you again or finishes.</p>",
                     action.session_id)
    messages = {
        "used": ("Already answered", "This button has already been used."),
        "expired": ("Link expired", "This button has expired. Open the run to answer."),
        "answered": ("Nothing to answer", "The run isn't waiting on this question any more: "
                     "it was answered in the app, or the run moved on or stopped."),
        "unknown": ("Link not recognised", "This link isn't valid. Open TWAIN to see your runs."),
    }
    title, text = messages.get(action.state, messages["unknown"])
    return _page(title, f"<p>{html.escape(text)}</p>", action.session_id)
