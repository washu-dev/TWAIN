"""Append-only provenance event log for module 14_provenance_memory.

Each event captures the context of one pipeline decision: the acting agent, the
input artifact, the output artifact, and the rationale. Events are persisted
one-per-line as JSON (JSONL) and hash-chained -- every event stores the sha256
of the previous event in ``prev_hash`` and the sha256 of its own canonical
content in ``hash`` -- so the log is tamper-evident and ``replay()`` can
deterministically reconstruct the recorded output artifacts.

``input_hash``/``output_hash`` are sha256 of the canonical ``inputs``/``outputs``,
which lets a re-run be verified against history (deterministic replay).

The log is append-only by construction: the public API only appends and reads;
there is no update or delete. Each appended event is validated against
schemas/provenance_event.schema.json.

Conforms to: schemas/provenance_event.schema.json (Story 1.5).
"""

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from jsonschema import Draft202012Validator

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "provenance_event.schema.json"

EVENT_TYPES = ("request", "plan", "execute", "validate", "correct", "approve")

# Field excluded from the per-event content hash (it stores that hash).
_HASH_FIELD = "hash"


def _canonical(obj) -> str:
    """Deterministic JSON serialization used for hashing.

    sort_keys + compact separators guarantee byte-identical output for equal
    content, so hashes are reproducible across processes.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha256(obj) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


class ProvenanceIntegrityError(Exception):
    """Raised when the hash chain of a provenance log fails verification."""


class EventLog:
    """Append-only, hash-chained provenance event log backed by a JSONL file."""

    def __init__(self, path, validate: bool = True):
        self.path = Path(path)
        self.validate = validate
        self._validator = Draft202012Validator(
            json.loads(_SCHEMA_PATH.read_text())
        )

    # ----------------------------------------------------------------- append
    def append(
        self,
        event_type: str,
        agent_id: str,
        inputs: Dict,
        outputs: Dict,
        decision_rationale: str = "",
    ) -> Dict:
        """Append a new decision event and return the stored record.

        ``input_hash``/``output_hash`` are derived from ``inputs``/``outputs``.
        ``prev_hash`` links to the current last event (empty for the first),
        and ``hash`` covers all other fields, chaining the event to history.
        """
        if event_type not in EVENT_TYPES:
            raise ValueError(f"event_type must be one of {EVENT_TYPES}")
        if type(inputs) is not dict:
            raise ValueError("inputs must be a dict")
        if type(outputs) is not dict:
            raise ValueError("outputs must be a dict")

        existing = self.read_all()
        seq = len(existing)
        prev_hash = existing[-1]["hash"] if existing else ""

        record = {
            "seq": seq,
            "event_id": uuid.uuid4().hex,
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "inputs": deepcopy(inputs),
            "outputs": deepcopy(outputs),
            "input_hash": _sha256(inputs),
            "output_hash": _sha256(outputs),
            "decision_rationale": decision_rationale,
            "prev_hash": prev_hash,
        }
        record[_HASH_FIELD] = _sha256(record)

        if self.validate:
            self._validator.validate(record)

        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        return deepcopy(record)

    # ------------------------------------------------------------------- read
    def read_all(self) -> List[Dict]:
        """Return all events in recorded order. Empty list if the log is absent."""
        if not self.path.exists():
            return []
        records: List[Dict] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    # --------------------------------------------------------------- integrity
    def verify_chain(self) -> bool:
        """Validate sequence, hash linkage, per-event hash, and input/output hashes.

        Returns True on success; raises ProvenanceIntegrityError otherwise.
        """
        prev_hash = ""
        for index, record in enumerate(self.read_all()):
            if record.get("seq") != index:
                raise ProvenanceIntegrityError(
                    f"seq mismatch at position {index}: got {record.get('seq')!r}"
                )
            if record.get("prev_hash") != prev_hash:
                raise ProvenanceIntegrityError(
                    f"prev_hash mismatch at seq {index}: chain is broken"
                )
            if record.get("input_hash") != _sha256(record.get("inputs")):
                raise ProvenanceIntegrityError(
                    f"input_hash mismatch at seq {index}: inputs were modified"
                )
            if record.get("output_hash") != _sha256(record.get("outputs")):
                raise ProvenanceIntegrityError(
                    f"output_hash mismatch at seq {index}: outputs were modified"
                )
            recomputed = _sha256({k: v for k, v in record.items() if k != _HASH_FIELD})
            if record.get(_HASH_FIELD) != recomputed:
                raise ProvenanceIntegrityError(
                    f"hash mismatch at seq {index}: event was modified after recording"
                )
            prev_hash = record[_HASH_FIELD]
        return True

    # ---------------------------------------------------------------- replay
    def replay(self) -> List[Dict]:
        """Reconstruct the recorded output artifacts in order.

        Verifies the chain first, so a tampered log cannot be silently replayed.
        Deterministic: replaying the same log always yields identical artifacts.
        """
        self.verify_chain()
        return [deepcopy(record["outputs"]) for record in self.read_all()]
