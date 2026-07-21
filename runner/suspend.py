"""The suspend signal that makes the runner asynchronous.

``SuspendRun`` is raised by the ``ask`` bridge (:class:`runner.bridges.DbAsk`)
when a run needs the researcher and no answer is waiting yet. It unwinds the
current state-machine step so the orchestrator can checkpoint the run as
*paused* and **release the process** — instead of blocking a thread that polls
the database for up to an hour (the old model). When the user replies, the API
enqueues a ``resume`` job; a runner rebuilds the orchestrator from the shared
session store and drives it forward, at which point the same ``ask`` call finds
the answer and returns it.

It is deliberately *not* an error: the orchestrator catches it before its error
handler, and :meth:`Orchestrator._advance` keeps it from tripping the circuit
breaker (a pause for input is progress, not a stage failure).

This module is kept dependency-free (no psycopg2, no ``modules/`` imports) so it
can be imported from any environment — the runner bridges, the engine adapter
that injects it into the orchestrator, and the unit tests — without the heavy
pixi environment on ``sys.path``.
"""


class SuspendRun(Exception):
    """Signal that a run must pause pending user input (not a failure).

    ``reason`` labels *why* we paused ("input" for a clarification/heavy-calc
    question, "approval" for the plan gate) so the notifier and event stream can
    tell the two apart.
    """

    def __init__(self, reason: str = "input", message: str = ""):
        super().__init__(message or f"run suspended pending user {reason}")
        self.reason = reason
