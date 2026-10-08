"""Shared-environment change proposals (#187): proposed by triage, approved by a
person, rolled out by the cluster monitor.

A shared RIS environment is used by every simulation, so TWAIN never changes one
on its own. When a run fails because a conda-only package is missing from the
env it ran in (triage's "environment / stop"), the worker drafts a proposal --
the env, the package (checked to exist on conda-forge), the spec before and
after, who it affects -- records it, and emails the approvers
(``TWAIN_ENV_APPROVERS``) Approve / Reject buttons (runner/email_actions.py).

On approval, :class:`EnvChangeScheduler` (in the cluster monitor's tick) submits
one RIS job running ``scripts/ris/rebuild_envs.sh`` with the proposed spec:
build ``.versions/<date>.p<id>/<env>`` beside the live version, verify it
(imports, binaries, functional checks, ACLs -- plus importing the new module),
and only then promote it. A failure at any point leaves the live env untouched.
On promotion the run's owner is emailed a "Re-run from EXECUTE" button, and the
RIS inventory is marked due so planning sees the new package.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("twain.env_proposals")

REPO = Path(__file__).resolve().parent.parent
SPECS = REPO / "scripts" / "ris" / "envs"


def approvers() -> list:
    raw = os.getenv("TWAIN_ENV_APPROVERS", "arifs@wustl.edu")
    return [a.strip() for a in raw.split(",") if a.strip()]


def conda_forge_has(package: str, *, timeout: float = 15.0) -> bool | None:
    """Whether conda-forge carries ``package`` (None: couldn't tell)."""
    url = f"https://api.anaconda.org/package/conda-forge/{package}"
    req = urllib.request.Request(url, headers={"User-Agent": "TWAIN (WashU)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except Exception:  # noqa: BLE001
        return None


def spec_with(spec: str, package: str) -> str:
    """``spec`` with ``package`` appended to its dependencies (comments kept)."""
    lines = spec.rstrip("\n").splitlines()
    last = max((i for i, ln in enumerate(lines) if re.match(r"\s+-\s+\S", ln)), default=None)
    entry = ("  # added by TWAIN proposal (#187): needed by a run that failed without it\n"
             f"  - {package}")
    if last is None:
        lines += ["dependencies:", entry]
    else:
        lines.insert(last + 1, entry)
    return "\n".join(lines) + "\n"


def declared(spec: str, package: str) -> bool:
    return any(re.match(rf"\s+-\s+{re.escape(package)}(\s|$|[=<>])", ln, re.I)
               for ln in spec.splitlines())


def propose(db, *, session_id: str, env: str, package: str, module: str, reason: str,
            base_url: str | None = None, check=conda_forge_has, send=None) -> dict | None:
    """Record a proposal and email the approvers; returns it (or None if not proposable)."""
    spec_path = SPECS / f"{env}.yml"
    if not spec_path.is_file():
        log.info("[env-change] no spec for env %r; nothing to propose", env)
        return None
    spec = spec_path.read_text(encoding="utf-8")
    if declared(spec, package):
        log.info("[env-change] %s already declares %s; nothing to propose", env, package)
        return None
    if check(package) is False:
        log.info("[env-change] %s isn't on conda-forge; nothing to propose", package)
        return None
    proposal = db.env_proposal_open(env, package)
    fresh = proposal is None
    if fresh:
        proposal = db.env_proposal_insert(session_id=session_id, env=env, package=package,
                                          module=module, reason=reason, spec_before=spec,
                                          spec_after=spec_with(spec, package))
    if fresh:
        from runner import email_actions, notifications
        base = (base_url or os.getenv("TWAIN_API_PUBLIC_URL", "")).rstrip("/")
        for approver in approvers():
            buttons = email_actions.issue_for_proposal(db, proposal, approver, base_url=base)
            subject = f"TWAIN: approve adding {package} to the shared {env} environment?"
            (send or notifications.send_email)(approver, subject, _approval_text(proposal), buttons)
    return proposal


def _approval_text(p: dict) -> str:
    return (
        f"A TWAIN run needs {p['package']} in the shared '{p['env']}' environment on RIS, "
        f"and it isn't there.\n\nWhy: {p['reason']}\n\n"
        f"The change: add '- {p['package']}' (conda-forge) to scripts/ris/envs/{p['env']}.yml.\n\n"
        f"Who it affects: every TWAIN run that uses the {p['env']} environment.\n\n"
        f"If you approve, TWAIN builds a new version beside the live one, verifies it "
        f"(imports, engine binaries, functional checks, permissions, and importing "
        f"{p.get('module') or p['package']}), and only then switches '{p['env']}' to it. "
        f"If anything fails, the live environment is untouched. Rollback afterwards is one "
        f"command: scripts/ris/rebuild_envs.sh rollback {p['env']} <previous version>.\n\n"
        f"Proposal #{p['id']}.")


def _job_script(p: dict, version: str, env_file: str | None) -> str:
    """One RIS job: build the proposed version, verify it, import the new module, promote."""
    ris = REPO / "scripts" / "ris"
    embed = []
    for path, text in ([("scripts/ris/rebuild_envs.sh", (ris / "rebuild_envs.sh").read_text()),
                        ("scripts/ris/provision_envs.sh", (ris / "provision_envs.sh").read_text())]
                       + [(f"scripts/ris/envs/{f.name}",
                           p["spec_after"] if f.stem == p["env"] else f.read_text())
                          for f in sorted(SPECS.glob("*.yml"))]):
        embed.append(f"cat > \"$W/{path}\" <<'TWAIN_EMBED_EOF'\n"
                     f"{text.rstrip(chr(10))}\nTWAIN_EMBED_EOF")
    module = p.get("module") or p["package"]
    env, export = p["env"], (f"export TWAIN_ENV_FILE='{env_file}'\n" if env_file else "")
    return (
        "#!/bin/bash\n" + export +
        "set -uo pipefail\n"
        '[ -r "${TWAIN_ENV_FILE:-}" ] && . "$TWAIN_ENV_FILE"\n'
        'W=$(mktemp -d); mkdir -p "$W/scripts/ris/envs"\n' + "\n".join(embed) + "\n"
        'R="$W/scripts/ris/rebuild_envs.sh"\n'
        f'bash "$R" build {version} {env} '
        '|| { echo "TWAIN_ENV_CHANGE: build failed"; exit 1; }\n'
        f'bash "$R" verify {version} {env} '
        '|| { echo "TWAIN_ENV_CHANGE: verify failed"; exit 1; }\n'
        f'P="$TWAIN_ENVS_ROOT/.versions/{version}/{env}"\n'
        f'( export PATH="$P/bin:$PATH" CONDA_PREFIX="$P"; python -c "import {module}" ) '
        f'&& echo "[verify] {env} imports {module} OK" '
        f'|| {{ echo "TWAIN_ENV_CHANGE: {module} does not import in the new version"; exit 1; }}\n'
        f'bash "$R" promote {version} {env} '
        f'&& echo "TWAIN_ENV_CHANGE: promoted {env} -> {version}"\n'
        'rm -rf "${W:?}"\n')


class EnvChangeScheduler:
    """Rolls out approved proposals (submit, follow, report) from the monitor's tick."""

    def __init__(self, db, adapter, profile, *, env_file: str | None = None, send=None):
        self.db = db
        self.adapter = adapter
        self.profile = profile
        self.env_file = env_file if env_file is not None else os.getenv("TWAIN_ENV_FILE")
        self.send = send

    def _send(self, *args):
        from runner import notifications
        (self.send or notifications.send_email)(*args)

    def tick(self) -> list:
        # Read both lists first: a job submitted this tick is followed from the next.
        approved = self.db.env_proposals_by_status("approved")
        building = self.db.env_proposals_by_status("building")
        return [self._submit(p) for p in approved] + [self._follow(p) for p in building]

    def _submit(self, p: dict) -> str:
        version = f"{datetime.date.today():%Y-%m-%d}.p{p['id']}"
        spec = {
            "script": _job_script(p, version, self.env_file),
            "job_name": f"twain-env-change-{p['id']}",
            "partition": self.profile.short_partition or self.profile.default_partition,
            "time_limit": "00:30:00", "nodes": 1, "ntasks": 1, "cpus_per_task": 4,
            "memory": "16384M", "account": self.profile.account,
        }
        try:
            job = self.adapter.submit(spec, idempotency_key=f"twain-env-change-{p['id']}")
        except Exception as exc:  # noqa: BLE001
            self.db.env_proposal_update(p["id"], status="failed", result=f"submit failed: {exc}")
            return "failed"
        self.db.env_proposal_update(p["id"], status="building", ris_job_id=str(job),
                                    version=version)
        log.info("[env-change] proposal %s: building %s/%s as Slurm job %s",
                 p["id"], p["env"], version, job)
        return "building"

    def _follow(self, p: dict) -> str:
        state = self.adapter.poll(p["ris_job_id"])
        if not state.is_terminal:
            return "running"
        out = self._stdout(p["ris_job_id"])
        promoted = f"TWAIN_ENV_CHANGE: promoted {p['env']} -> {p['version']}" in out
        marks = ("[verify]", "[promote]", "TWAIN_ENV_CHANGE")
        tail = "\n".join(ln for ln in out.splitlines() if ln.startswith(marks))[-3000:]
        self.db.env_proposal_update(p["id"], status="promoted" if promoted else "failed",
                                    result=tail)
        for approver in approvers():
            self._send(approver,
                       f"TWAIN: {p['package']} "
                       f"{'is now in' if promoted else 'could NOT be added to'} "
                       f"the shared {p['env']} environment",
                       (f"Proposal #{p['id']} was rolled out: '{p['env']}' now points at version "
                        f"{p['version']}.\n\nOne thing left for a person: commit the new spec "
                        f"to the repository, or the next full rebuild drops {p['package']}. "
                        f"Add these lines to scripts/ris/envs/{p['env']}.yml:\n"
                        f"  - {p['package']}" if promoted else
                        f"Proposal #{p['id']} failed; the live '{p['env']}' environment was not "
                        f"changed.") + f"\n\nWhat the job reported:\n{tail}", [])
        if promoted:
            self.db.inventory_mark_due()
            self._offer_rerun(p)
        return "promoted" if promoted else "failed"

    def _offer_rerun(self, p: dict) -> None:
        if not p.get("session_id"):
            return
        from runner import email_actions
        try:
            owner = self.db.owner_contact(p["session_id"]) or {}
        except Exception:  # noqa: BLE001
            owner = {}
        if not owner.get("email"):
            return
        buttons = email_actions.issue_rerun(self.db, p["session_id"], owner["email"])
        self._send(owner["email"],
                   f"TWAIN: {p['package']} is now available -- re-run your calculation?",
                   f"Your run stopped because the '{p['env']}' environment lacked {p['package']}. "
                   f"That's been added (proposal #{p['id']}, approved and verified). Re-run from "
                   f"EXECUTE to compute it now.", buttons)

    def _stdout(self, job_id: str) -> str:
        chunks, offset = [], 0
        for _ in range(64):
            page = self.adapter.stdout_page(job_id, offset, 65_536)
            chunks.append(page.get("content") or "")
            nxt = page.get("next_offset", offset)
            if page.get("eof") or nxt <= offset:
                break
            offset = nxt
        return "".join(chunks)


def proposal_from_failure(failure: dict | None) -> dict | None:
    """What to propose from a run's failure, if triage stopped for a shared env."""
    history = (failure or {}).get("self_heal") or []
    last = history[-1] if history else None
    if not last or last.get("class") != "environment" or last.get("action") != "stop":
        return None
    match = re.search(r"'([A-Za-z0-9_.\-]+)' is missing", str(last.get("reason") or ""))
    env = (failure or {}).get("env")
    if not match or not env:
        return None
    return {"module": match.group(1), "env": env, "reason": json.dumps(last.get("reason"))[1:-1]}
