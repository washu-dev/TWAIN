"""HTTP client for the WashU RIS API (the hosted Slurm-on-Compute2 gateway).

RIS (Research Infrastructure Services) publishes a REST API in front of the
same Compute2 Slurm cluster :mod:`slurm_adapter` talks to over SSH:
submit/list/get/cancel a job, pull its accounting/stdout/stderr, list
nodes/partitions. :class:`RisApiClient` is a thin wrapper around the handful
of endpoints :class:`~execution_adapter.ris_api_adapter.RisApiAdapter` needs --
not a full SDK for the whole surface (identity/token/recipe management is out
of scope; TWAIN uses one long-lived service PAT, not per-researcher accounts).

Follows this repo's one existing outbound-HTTPS convention
(:class:`~AgentInterface.AgentInterface`'s use of ``requests``) rather than
introducing a new HTTP library. The transport (a ``requests.Session``-like
object) is injectable so callers can unit-test offline, mirroring the
``Runner`` callable seam :mod:`slurm_adapter` uses for SSH.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests

DEFAULT_BASE_URL = "https://d3n2m687w2hvtj.cloudfront.net/api/v1"
DEFAULT_TIMEOUT = 30.0


class RisApiError(RuntimeError):
    """Raised when a RIS API call fails (network error, non-2xx, bad body).

    ``status`` is the HTTP status code, or None when no response arrived
    (network error/timeout). ``code`` is ris-api's machine-readable error code
    (e.g. ``VALIDATION_ERROR``) when the body carried one. ``transient`` tells
    a failure worth retrying (no response, 429, 5xx, or a 2xx whose body isn't
    the JSON ris-api always sends -- e.g. a CloudFront error page) from one
    that retrying can't fix (any other 4xx).
    """

    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: Optional[str] = None, transient: Optional[bool] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self._transient = transient

    @property
    def transient(self) -> bool:
        if self._transient is not None:
            return self._transient
        return self.status is None or self.status == 429 or self.status >= 500

    @property
    def auth_failure(self) -> bool:
        return self.status in (401, 403)


def _error_text(resp) -> tuple:
    """``(code, message)`` from a non-2xx response.

    ris-api's envelope is ``{"error": {"code", "message", "correlation_id",
    "details"}}``; the correlation id is kept so a failure can be traced in
    ris-api's logs. Anything else (FastAPI's ``detail``, a CloudFront HTML
    page, an empty body) degrades to the best text available.
    """
    text = (resp.text or "").strip()
    try:
        body = resp.json()
    except ValueError:
        return None, text[:300] or getattr(resp, "reason", "") or "no response body"
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        message = str(err.get("message") or err.get("code") or text[:300])
        if err.get("correlation_id"):
            message += f" (correlation_id {err['correlation_id']})"
        return err.get("code"), message
    if isinstance(body, dict) and body.get("detail"):
        return None, str(body["detail"])
    return None, text[:300]


class RisApiClient:
    """Authenticated client for the six RIS API endpoints the adapter needs."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """``base_url``/``token`` default to ``RIS_API_BASE_URL``/``RIS_API_TOKEN``
        env vars. ``session`` is injectable (any object exposing ``.request()``
        with the ``requests`` signature) so tests never hit the network.

        The token is not required at construction time -- only when a call is
        actually made (see :meth:`_headers`). This lets a caller build a
        client (or an adapter that holds one) before deciding whether to use
        it, and lets a missing token surface as an ordinary submit-time
        ``RisApiError`` rather than a crash at construction."""
        self.base_url = (base_url or os.environ.get("RIS_API_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.token = token or os.environ.get("RIS_API_TOKEN")
        self.session = session or requests.Session()
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        if not self.token:
            raise RisApiError(
                "no RIS API token: set RIS_API_TOKEN (a bearer PAT from the "
                "RIS API) in the environment/secret store"
            )
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    # --------------------------------------------------------------- transport
    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            resp = self.session.request(
                method, url, headers=headers, timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise RisApiError(f"{method} {path} failed: {exc}") from exc
        if resp.status_code >= 400:
            code, message = _error_text(resp)
            if resp.status_code in (401, 403):
                message += (" -- check RIS_API_TOKEN (the PAT may be expired, "
                            "revoked, or lack cluster access)")
            raise RisApiError(
                f"{method} {path} returned {resp.status_code}: {message}",
                status=resp.status_code, code=code,
            )
        return resp

    def _json(self, method: str, path: str, *required: str, **kwargs) -> Dict[str, Any]:
        """:meth:`_request`, then the body as a JSON object holding ``required``.

        A 2xx that isn't such an object (an HTML page from CloudFront, an
        empty body, a list) raises a transient :class:`RisApiError` instead of
        leaking ``ValueError``/``KeyError`` past the adapter's error handling.
        """
        resp = self._request(method, path, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = None
        missing = [k for k in required if not isinstance(body, dict) or k not in body]
        if not isinstance(body, dict) or missing:
            snippet = (resp.text or "").strip()[:200] or "empty body"
            raise RisApiError(
                f"{method} {path} returned {resp.status_code} with an unexpected "
                f"body (missing {', '.join(missing) or 'a JSON object'}): {snippet}",
                status=resp.status_code, transient=True,
            )
        return body

    # -------------------------------------------------------------------- jobs
    def submit_job(self, spec: Dict[str, Any], *, idempotency_key: Optional[str] = None) -> str:
        """``POST /jobs`` -- returns the new job id.

        ``idempotency_key`` (sent as the ``Idempotency-Key`` header) lets a
        retried call after a network blip avoid double-submitting the same
        job. Use one fresh key per genuine submission: ris-api keeps keys
        forever and never compares bodies, so a reused key returns the old job.
        """
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._json("POST", "/jobs", "job_id", json=spec, headers=headers)["job_id"]

    def preview_job(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /jobs/preview`` -- ``sbatch --test-only`` dry run, no job queued."""
        return self._json("POST", "/jobs/preview", json=spec)

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """``GET /jobs/{id}`` -- current state + scheduler detail."""
        return self._json("GET", f"/jobs/{job_id}", "state")

    def cancel_job(self, job_id: str, *, signal: Optional[str] = None) -> None:
        """``DELETE /jobs/{id}`` -- ``scancel`` (optionally with a signal)."""
        body = {"signal": signal} if signal else None
        self._request("DELETE", f"/jobs/{job_id}", json=body)

    def accounting(self, job_id: str) -> Dict[str, Any]:
        """``GET /jobs/{id}/accounting`` -- sacct-style final accounting."""
        return self._json("GET", f"/jobs/{job_id}/accounting", "state")

    def stdout(self, job_id: str) -> str:
        """``GET /jobs/{id}/stdout`` -- the job's captured standard output."""
        return self._json("GET", f"/jobs/{job_id}/stdout", "content")["content"]

    def stderr(self, job_id: str) -> str:
        """``GET /jobs/{id}/stderr`` -- the job's captured standard error."""
        return self._json("GET", f"/jobs/{job_id}/stderr", "content")["content"]

    def output_page(self, job_id: str, stream: str, *, offset: int = 0,
                    limit: int = 65_536) -> Dict[str, Any]:
        """``GET /jobs/{id}/output/{stream}?offset=N`` -- one page from a byte
        cursor: ``{content, offset, next_offset, size, eof, job_finished, ...}``.
        Following a live job means calling again with ``offset=next_offset``;
        a page never ends mid-character."""
        return self._json("GET", f"/jobs/{job_id}/output/{stream}",
                          "content", "next_offset",
                          params={"offset": offset, "limit": limit})

    def output_tail(self, job_id: str, stream: str, nbytes: int) -> str:
        """``GET /jobs/{id}/output/{stream}?tail=N`` -- the last ``nbytes`` of
        ``stream`` (``"stdout"``/``"stderr"``), never split mid-character.
        The whole-file endpoints above are silently capped at 8 MB; this one
        is bounded by ``nbytes`` (ris-api allows up to 1 MiB)."""
        return self._json("GET", f"/jobs/{job_id}/output/{stream}", "content",
                          params={"tail": nbytes, "limit": nbytes})["content"]
