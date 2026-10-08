"""One-tap gate answers from email: the buttons in an approval/confirmation email.

When the worker emails a gate -- the plan approval, the heavy-calculation
confirmation, the accept-or-rerun question -- it issues one token per choice and
puts each in a button. The button opens ``/api/actions/<token>`` on the API: a
small confirmation page (one more tap: WashU mail runs Microsoft link scanning,
which OPENS every link in a message, so a link that acted on GET would let the
scanner approve runs), whose form POST answers the gate exactly as the app
would, with no sign-in. Tokens are random, stored only as SHA-256, bound to that
one gate message, single-use (siblings are spent too) and expire after
``TWAIN_EMAIL_ACTION_HOURS`` (72). Clarification questions want free text, so
they keep the link to the run.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets

log = logging.getLogger("twain.email_actions")

#: gate message kind -> [(choice sent to the gate, button label, primary?)]
GATES = {
    "approval_request": [("approve", "Approve plan", True), ("reject", "Reject", False)],
    "heavy_confirm": [("yes", "Run it now", True), ("no", "Not now", False)],
    "validation_gate": [("accept", "Accept result", True), ("rerun", "Try a correction", False)],
}


def hours() -> float:
    return float(os.getenv("TWAIN_EMAIL_ACTION_HOURS", "72"))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(db, session_id: str, *, base_url: str | None = None) -> list:
    """Buttons for the gate this run is waiting on: ``[{label, url, primary}]``.

    Empty when the run isn't parked at a yes/no gate, or no public API URL is
    configured -- the email then carries only the link to the run, as before.
    Never raises: a button is a convenience, the email must still go out.
    """
    base = (base_url or os.getenv("TWAIN_API_PUBLIC_URL", "")).strip().rstrip("/")
    if not base:
        return []
    try:
        gate = db.pending_gate(session_id)
        if not gate or gate.get("kind") not in GATES:
            return []
        rows, buttons = [], []
        for choice, label, primary in GATES[gate["kind"]]:
            token = secrets.token_urlsafe(32)
            rows.append((token_hash(token), session_id, gate["id"], gate["kind"], choice, label))
            buttons.append({"label": label, "url": f"{base}/api/actions/{token}",
                            "primary": primary})
        db.insert_email_actions(rows, hours())
        return buttons
    except Exception as exc:  # noqa: BLE001 - the email still goes, without buttons
        log.warning("[notify] could not issue email actions for %s: %s", session_id, exc)
        return []


def _buttons(db, rows_spec, base: str, valid_hours: float) -> list:
    rows, buttons = [], []
    for (session_id, kind, choice, label, primary, proposal_id, recipient) in rows_spec:
        token = secrets.token_urlsafe(32)
        rows.append((token_hash(token), session_id, kind, choice, label, proposal_id, recipient))
        buttons.append({"label": label, "url": f"{base}/api/actions/{token}", "primary": primary})
    db.insert_email_action_rows(rows, valid_hours)
    return buttons


def issue_for_proposal(db, proposal: dict, approver: str, *, base_url: str | None = None) -> list:
    """Approve / Reject buttons for a shared-environment change (#187), for one approver."""
    base = (base_url or os.getenv("TWAIN_API_PUBLIC_URL", "")).strip().rstrip("/")
    if not base or not proposal.get("session_id"):
        return []
    try:
        sid, pid = str(proposal["session_id"]), proposal["id"]
        return _buttons(db, [
            (sid, "env_change", "approve", "Approve the change", True, pid, approver),
            (sid, "env_change", "reject", "Reject", False, pid, approver)], base, hours())
    except Exception as exc:  # noqa: BLE001 - the email still goes, without buttons
        log.warning("[env-change] could not issue buttons for proposal %s: %s",
                    proposal.get("id"), exc)
        return []


def issue_rerun(db, session_id: str, owner: str, *, base_url: str | None = None) -> list:
    """A "Re-run from EXECUTE" button for a run whose environment was just fixed."""
    base = (base_url or os.getenv("TWAIN_API_PUBLIC_URL", "")).strip().rstrip("/")
    if not base:
        return []
    try:
        return _buttons(db, [(session_id, "rerun", "EXECUTE", "Re-run from EXECUTE", True,
                              None, owner)],
                        base, hours())
    except Exception as exc:  # noqa: BLE001
        log.warning("[env-change] could not issue a re-run button for %s: %s", session_id, exc)
        return []

