"""RIS inventory (P1, #185): plan from what the cluster envs actually contain.

The spec files (scripts/ris/envs/*.yml) say what an env *should* hold; runs kept
failing on the difference -- an env promised by its spec but never provisioned
where the job looked (e825d5ed), an env whose packages drifted from its spec.
So a read-only Slurm job (scripts/ris/inventory.sh) lists every env's installed
packages, and planning uses that instead.

* :class:`InventoryScheduler` runs inside the cluster monitor's tick (one leader
  across workers): it submits the job when the newest inventory is older than
  ``TWAIN_INVENTORY_HOURS`` (default 24), and ingests the output when it ends.
* :func:`apply_latest` points the state machine's env view at the newest
  inventory (falling back to the specs when there is none, or it is older than
  ``TWAIN_INVENTORY_MAX_AGE_HOURS``, default 168). The runner calls it before
  every slice; the worker calls it at start and after each ingest.

    TWAIN_ENV_FILE=<twain.sh> bash scripts/ris/inventory.sh   # the job, by hand
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

log = logging.getLogger("twain.inventory")

MARKER = "TWAIN_INVENTORY_JSON: "
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ris" / "inventory.sh"


def every_hours() -> float:
    return float(os.getenv("TWAIN_INVENTORY_HOURS", "24"))


def max_age_hours() -> float:
    return float(os.getenv("TWAIN_INVENTORY_MAX_AGE_HOURS", "168"))


def job_spec(profile, env_file: str | None) -> dict:
    """The inventory job: a minute of one CPU on the profile's short partition."""
    body = SCRIPT.read_text(encoding="utf-8").split("\n", 1)[1]   # drop the shebang
    export = f"export TWAIN_ENV_FILE='{env_file}'\n" if env_file else ""
    return {
        "script": "#!/bin/bash\n" + export + body,
        "job_name": "twain-inventory",
        "partition": profile.short_partition or profile.default_partition,
        "time_limit": "00:10:00",
        "nodes": 1, "ntasks": 1, "cpus_per_task": 1, "memory": "2048M",
        "account": profile.account,
    }


def parse(stdout: str) -> dict:
    """The snapshot from the job's stdout (ValueError when it isn't there)."""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(MARKER):
            snapshot = json.loads(line[len(MARKER):])
            if not isinstance(snapshot.get("envs"), dict):
                raise ValueError("inventory has no envs mapping")
            return snapshot
    raise ValueError("inventory output has no TWAIN_INVENTORY_JSON line")


def envs_for_planning(envs: dict) -> dict:
    """``{env: [packages]}`` for the state machine; None for an env it couldn't read."""
    return {name: (None if (e or {}).get("error") else sorted((e or {}).get("packages") or {}))
            for name, e in (envs or {}).items()}


def apply_latest(db) -> str:
    """Point planning at the newest inventory (or the specs). Returns the source used."""
    try:
        import statemachine
    except ImportError:          # pipeline modules not on the path (bare tests)
        return "unavailable"
    row = None
    try:
        row = db.latest_inventory(max_age_hours())
    except Exception as exc:  # noqa: BLE001 - planning falls back to the specs
        log.warning("[inventory] could not read the latest inventory: %s", exc)
    envs = (row or {}).get("envs") or None
    taken = str(row["taken_at"]) if row and row.get("taken_at") else None
    statemachine.use_cluster_inventory(envs_for_planning(envs) if envs else None, taken)
    return statemachine.cluster_env_source()


class InventoryScheduler:
    """Submits the inventory job when it's due and ingests it when it ends."""

    def __init__(self, db, adapter, profile, *, env_file: str | None = None,
                 on_ingest=None, page_bytes: int = 65_536):
        self.db = db
        self.adapter = adapter
        self.profile = profile
        self.env_file = env_file if env_file is not None else os.getenv("TWAIN_ENV_FILE")
        self.on_ingest = on_ingest
        self.page_bytes = page_bytes

    def tick(self) -> str:
        """One pass: collect a finished job, or submit one if due. Returns what happened."""
        pending = self.db.inventory_pending()
        if pending:
            return self._collect(pending)
        if not self.db.inventory_due(every_hours()):
            return "fresh"
        return self._submit()

    def _submit(self) -> str:
        inventory_id = self.db.inventory_submitting()
        try:
            job_id = self.adapter.submit(job_spec(self.profile, self.env_file),
                                         idempotency_key=f"twain-inventory-{inventory_id}-{uuid.uuid4().hex[:8]}")
        except Exception as exc:  # noqa: BLE001 - recorded; retried after the interval
            self.db.inventory_failed(inventory_id, f"submit failed: {exc}")
            log.warning("[inventory] submit failed: %s", exc)
            return "submit-failed"
        self.db.inventory_set_job(inventory_id, job_id)
        log.info("[inventory] submitted Slurm job %s", job_id)
        return "submitted"

    def _collect(self, pending: dict) -> str:
        job_id = pending.get("ris_job_id")
        if not job_id:                      # the submit never answered; give up on it
            self.db.inventory_failed(pending["id"], "no Slurm job id was recorded")
            return "failed"
        state = self.adapter.poll(job_id)
        if not state.is_terminal:
            return "running"
        try:
            snapshot = parse(self._stdout(job_id))
        except Exception as exc:  # noqa: BLE001
            self.db.inventory_failed(pending["id"], f"Slurm job {job_id} ({state.value}): {exc}")
            log.warning("[inventory] job %s gave no inventory: %s", job_id, exc)
            return "failed"
        self.db.inventory_ingested(pending["id"], snapshot)
        log.info("[inventory] ingested job %s: %d envs, %d modules", job_id,
                 len(snapshot["envs"]), len(snapshot.get("modules") or []))
        if self.on_ingest:
            try:
                self.on_ingest()
            except Exception as exc:  # noqa: BLE001 - the snapshot is stored either way
                log.warning("[inventory] post-ingest hook failed: %s", exc)
        return "ingested"

    def _stdout(self, job_id: str) -> str:
        """The job's whole stdout, page by page (the snapshot is one long line)."""
        chunks, offset = [], 0
        for _ in range(64):
            page = self.adapter.stdout_page(job_id, offset, self.page_bytes)
            chunks.append(page.get("content") or "")
            nxt = page.get("next_offset", offset)
            if page.get("eof") or nxt <= offset:
                break
            offset = nxt
        return "".join(chunks)
