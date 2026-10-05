"""Unit tests for the RIS-API-backed job-control adapter (module 08).

All HTTP interaction goes through an injected fake client, so these tests
exercise the real rendering / partition-selection / submit-poll-cancel logic
fully offline -- no network, no live RIS API.

Run from the repo root with:  pixi run pytest tests/unit/test_ris_api_adapter.py
"""
import pytest
from execution_adapter.cluster_profile import ClusterProfile
from execution_adapter.ris_api_adapter import RisApiAdapter
from execution_adapter.ris_api_client import RisApiError
from execution_adapter.slurm_adapter import JobSpec, JobState, SlurmError
from plan_synthesizer.execution_plan import SlurmRequest

# ── fixtures ──────────────────────────────────────────────────────────────────

def _profile():
    return ClusterProfile(
        name="compute2",
        login_nodes=["c2-login-001.ris.wustl.edu"],
        account="compute2-mdan",
        accounts=["compute2-mdan", "compute2-workshop"],
        modules=["ris", "slurm"],
        gpu_modules=["py-torch"],
        default_partition="general-cpu",
        gpu_partition="general-gpu",
        short_partition="general-short",
        partitions=[
            {"name": "general-cpu", "max_minutes": 21600, "gpus": False},
            {"name": "general-gpu", "max_minutes": 21600, "gpus": True},
            {"name": "general-bigmem", "max_minutes": 21600, "gpus": False},
            {"name": "general-short", "max_minutes": 30, "gpus": True},
        ],
    )


class FakeClient:
    """Records calls and returns scripted results/errors per method."""

    def __init__(self, **overrides):
        self.calls = []
        self._overrides = overrides

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name in self._overrides:
            result = self._overrides[name]
            if isinstance(result, Exception):
                raise result
            return result
        return None

    def submit_job(self, spec, *, idempotency_key=None):
        return self._record("submit_job", spec, idempotency_key=idempotency_key) or "42"

    def get_job(self, job_id):
        return self._record("get_job", job_id) or {"job_id": job_id, "state": "RUNNING"}

    def cancel_job(self, job_id, *, signal=None):
        self._record("cancel_job", job_id, signal=signal)

    def accounting(self, job_id):
        return self._record("accounting", job_id) or {}

    def stdout(self, job_id):
        return self._record("stdout", job_id) or ""

    def stderr(self, job_id):
        return self._record("stderr", job_id) or ""

    def output_tail(self, job_id, stream, nbytes):
        return self._record("output_tail", job_id, stream, nbytes) or ""


# ── partition selection (shared policy with SlurmAdapter) ───────────────────

def test_select_partition_prefers_gpu_for_gpu_jobs():
    adapter = RisApiAdapter(_profile(), client=FakeClient())
    req = SlurmRequest(cpu_count=4, gpu_count=2, max_time=120, ram=8000)
    assert adapter.select_partition(req).name == "general-gpu"


def test_select_partition_override_validated():
    adapter = RisApiAdapter(_profile(), client=FakeClient())
    req = SlurmRequest(cpu_count=1, gpu_count=0, max_time=600, ram=2000)
    with pytest.raises(SlurmError):
        adapter.select_partition(req, override="general-short")  # 600 > 30


# ── job-spec rendering ────────────────────────────────────────────────────────

def test_render_job_spec_cpu_job():
    adapter = RisApiAdapter(_profile(), client=FakeClient())
    job = JobSpec(job_name="solub", command=["python", "main.py"],
                  workdir="/storage2/fs1/me/run")
    req = SlurmRequest(cpu_count=8, gpu_count=0, max_time=45, ram=16000)
    spec = adapter.render_job_spec(job, req)

    assert spec["job_name"] == "solub"
    assert spec["partition"] == "general-cpu"      # 45 > short cap -> general-cpu
    assert spec["account"] == "compute2-mdan"
    assert spec["cpus_per_task"] == 8
    assert spec["memory"] == "16000M"
    assert spec["time_limit"] == "00:45:00"
    assert spec["nodes"] == 1 and spec["ntasks"] == 1
    assert "gpus" not in spec                       # no GPU key for a CPU job
    assert spec["working_dir"] == "/storage2/fs1/me/run"
    assert "module load ris slurm >/dev/null 2>&1 || true" in spec["script"]
    assert "cd /storage2/fs1/me/run" in spec["script"]
    assert spec["script"].rstrip().endswith("python main.py")
    assert 'export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-1}"' in spec["script"]


def test_render_job_spec_job_env_overrides_threading_defaults():
    adapter = RisApiAdapter(_profile(), client=FakeClient())
    job = JobSpec(job_name="solub", command="python main.py",
                  env={"OMP_NUM_THREADS": "2"})
    req = SlurmRequest(cpu_count=8, gpu_count=0, max_time=45, ram=16000)
    spec = adapter.render_job_spec(job, req)
    assert "export OMP_NUM_THREADS=2" in spec["script"]
    assert 'export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-1}"' not in spec["script"]
    assert 'export MKL_NUM_THREADS="${SLURM_CPUS_PER_TASK:-1}"' in spec["script"]


def test_render_job_spec_gpu_job_sets_gpus_and_modules():
    adapter = RisApiAdapter(_profile(), client=FakeClient())
    job = JobSpec(job_name="md", command="python md.py")
    req = SlurmRequest(cpu_count=4, gpu_count=2, max_time=120, ram=32000)
    spec = adapter.render_job_spec(job, req)

    assert spec["partition"] == "general-gpu"
    assert spec["gpus"] == 2
    assert "py-torch" in spec["script"]              # gpu module folded in


def test_render_job_spec_formats_walltime_over_a_day():
    adapter = RisApiAdapter(_profile(), client=FakeClient())
    job = JobSpec(job_name="j", command="true")
    long = SlurmRequest(cpu_count=1, gpu_count=0, max_time=1500, ram=1000)
    assert adapter.render_job_spec(job, long)["time_limit"] == "1-01:00:00"


def test_a_pinned_gpu_type_is_rejected_not_silently_dropped():
    # The API takes only a GPU count; quietly running on any GPU would be
    # wrong for a profile that pins e.g. H100s (#154).
    profile = _profile()
    profile.gpu_type = "h100"
    adapter = RisApiAdapter(profile, client=FakeClient())
    job = JobSpec(job_name="j", command="python main.py")
    with pytest.raises(SlurmError, match="TWAIN_SLURM_BACKEND=ssh"):
        adapter.render_job_spec(job, SlurmRequest(cpu_count=4, gpu_count=1, max_time=60, ram=8000))
    # A CPU job on the same profile is unaffected.
    spec = adapter.render_job_spec(job, SlurmRequest(cpu_count=4, gpu_count=0, max_time=60, ram=8000))
    assert "gpus" not in spec


# ── submit / poll / cancel / accounting / logs ───────────────────────────────

def test_submit_returns_job_id_and_forwards_idempotency_key():
    client = FakeClient(submit_job="99")
    adapter = RisApiAdapter(_profile(), client=client)
    job_id = adapter.submit({"job_name": "x"}, idempotency_key="run-1")
    assert job_id == "99"
    name, _args, kwargs = client.calls[0]
    assert name == "submit_job" and kwargs["idempotency_key"] == "run-1"


class FlakySubmitClient(FakeClient):
    """submit_job raises the scripted errors in order, then succeeds."""

    def __init__(self, *errors):
        super().__init__()
        self._errors = list(errors)

    def submit_job(self, spec, *, idempotency_key=None):
        self.calls.append(("submit_job", (spec,), {"idempotency_key": idempotency_key}))
        if self._errors:
            raise self._errors.pop(0)
        return "77"


@pytest.mark.parametrize("error", [
    RisApiError("connection reset"),                  # no response at all
    RisApiError("busy", status=503),
    RisApiError("slow down", status=429),
])
def test_submit_resends_a_transient_failure_with_the_same_key(error):
    client = FlakySubmitClient(error)
    adapter = RisApiAdapter(_profile(), client=client)
    slept = []

    job_id = adapter.submit({"job_name": "x"}, idempotency_key="k-1", sleep=slept.append)

    assert job_id == "77"
    assert [c[2]["idempotency_key"] for c in client.calls] == ["k-1", "k-1"]
    assert slept == [RisApiAdapter.SUBMIT_RETRY_BACKOFF[0]]


def test_submit_gives_up_after_the_bounded_retries():
    errors = [RisApiError("down", status=502)] * 5
    client = FlakySubmitClient(*errors)
    adapter = RisApiAdapter(_profile(), client=client)

    with pytest.raises(SlurmError, match="down"):
        adapter.submit({"job_name": "x"}, idempotency_key="k-1", sleep=lambda _s: None)
    assert len(client.calls) == len(RisApiAdapter.SUBMIT_RETRY_BACKOFF) + 1


def test_submit_does_not_retry_a_rejected_spec():
    client = FlakySubmitClient(RisApiError("invalid partition", status=422))
    adapter = RisApiAdapter(_profile(), client=client)

    with pytest.raises(SlurmError, match="invalid partition"):
        adapter.submit({"job_name": "x"}, idempotency_key="k-1", sleep=lambda _s: None)
    assert len(client.calls) == 1


def test_submit_without_a_key_never_resends():
    # A re-send without a key could queue the job twice.
    client = FlakySubmitClient(RisApiError("connection reset"))
    adapter = RisApiAdapter(_profile(), client=client)

    with pytest.raises(SlurmError):
        adapter.submit({"job_name": "x"}, sleep=lambda _s: None)
    assert len(client.calls) == 1


def test_submit_wraps_api_errors_as_slurm_error():
    client = FakeClient(submit_job=RisApiError("boom"))
    adapter = RisApiAdapter(_profile(), client=client)
    with pytest.raises(SlurmError):
        adapter.submit({"job_name": "x"})


def test_poll_maps_api_state_to_job_state():
    client = FakeClient(get_job={"job_id": "42", "state": "COMPLETED"})
    adapter = RisApiAdapter(_profile(), client=client)
    assert adapter.poll("42") == JobState.COMPLETED


def test_poll_wraps_api_errors_as_slurm_error():
    client = FakeClient(get_job=RisApiError("timeout"))
    adapter = RisApiAdapter(_profile(), client=client)
    with pytest.raises(SlurmError):
        adapter.poll("42")


def test_cancel_invokes_client():
    client = FakeClient()
    adapter = RisApiAdapter(_profile(), client=client)
    adapter.cancel("42")
    assert client.calls[-1][0] == "cancel_job"


def test_accounting_normalizes_field_names_and_shape():
    client = FakeClient(accounting={
        "job_id": "42", "state": "COMPLETED", "exit_code": "0:0",
        "elapsed": "00:05:00", "max_rss": "512000K",
    })
    adapter = RisApiAdapter(_profile(), client=client)
    acct = adapter.accounting("42")
    assert acct == {"State": "COMPLETED", "ExitCode": "0:0",
                    "Elapsed": "00:05:00", "MaxRSS": "512000K"}


def test_accounting_omits_empty_fields():
    client = FakeClient(accounting={"job_id": "42", "state": "PENDING"})
    adapter = RisApiAdapter(_profile(), client=client)
    assert adapter.accounting("42") == {"State": "PENDING"}


def test_exit_code_parses_colon_format():
    client = FakeClient(accounting={"job_id": "42", "state": "FAILED", "exit_code": "1:0"})
    adapter = RisApiAdapter(_profile(), client=client)
    assert adapter.exit_code("42") == 1


def test_exit_code_none_when_missing():
    client = FakeClient(accounting={"job_id": "42", "state": "PENDING"})
    adapter = RisApiAdapter(_profile(), client=client)
    assert adapter.exit_code("42") is None


def test_stdout_and_stderr_fetch_content():
    client = FakeClient(stdout="hello\n", output_tail="oops\n")
    adapter = RisApiAdapter(_profile(), client=client)
    assert adapter.stdout("42") == "hello\n"
    assert adapter.stderr("42") == "oops\n"
    # stderr is read as a bounded tail, where the traceback lives.
    assert client.calls[-1] == (
        "output_tail", ("42", "stderr", RisApiAdapter.STDERR_TAIL_BYTES), {})


# ── wait ──────────────────────────────────────────────────────────────────────

def test_wait_polls_until_terminal():
    states = iter(["PENDING", "RUNNING", "COMPLETED"])
    client = FakeClient()
    client.get_job = lambda job_id: {"job_id": job_id, "state": next(states)}
    adapter = RisApiAdapter(_profile(), client=client)
    sleeps = []
    result = adapter.wait("42", poll_interval=1.0, sleep=sleeps.append)
    assert result == JobState.COMPLETED
    assert sleeps == [1.0, 1.0]


def test_wait_raises_when_max_wait_expires_while_still_running():
    client = FakeClient(get_job={"job_id": "42", "state": "RUNNING"})
    adapter = RisApiAdapter(_profile(), client=client)
    with pytest.raises(SlurmError, match="still running"):
        adapter.wait("42", poll_interval=1.0, max_wait=1.0, sleep=lambda _s: None)


def test_wait_tolerates_transient_failures_then_raises_after_the_budget():
    client = FakeClient(get_job=RisApiError("network blip"))
    adapter = RisApiAdapter(_profile(), client=client)
    adapter.CONTACT_LOSS_TOLERANCE = 2.0
    with pytest.raises(SlurmError, match="lost contact with the RIS API"):
        adapter.wait("42", poll_interval=1.0, sleep=lambda _s: None)



def test_wait_fails_fast_on_a_rejected_token():
    # An expired/revoked PAT won't recover mid-wait: no 30-minute grace (#152).
    client = FakeClient(get_job=RisApiError("401: bad token", status=401))
    adapter = RisApiAdapter(_profile(), client=client)
    sleeps = []
    with pytest.raises(SlurmError, match="bad token"):
        adapter.wait("42", poll_interval=1.0, sleep=sleeps.append)
    assert sleeps == []


def test_poll_falls_back_to_accounting_once_the_job_ages_out():
    client = FakeClient(get_job=RisApiError("not found", status=404),
                        accounting={"job_id": "42", "state": "COMPLETED"})
    adapter = RisApiAdapter(_profile(), client=client)
    assert adapter.poll("42") == JobState.COMPLETED


def test_wait_rides_out_a_cloudfront_html_page():
    # A 200 whose body is CloudFront's HTML must count as lost contact, not
    # escape wait() as a ValueError and crash the run (#152).
    from execution_adapter.ris_api_client import RisApiClient

    class Resp:
        def __init__(self, status, body=None, text=""):
            self.status_code, self._body, self.text = status, body, text

        def json(self):
            if self._body is None:
                raise ValueError("not json")
            return self._body

    replies = iter([
        Resp(200, text="<html><body>502 Bad Gateway</body></html>"),
        Resp(200, {"job_id": "42", "state": "COMPLETED"}),
    ])

    class Session:
        def request(self, method, url, **kwargs):
            return next(replies)

    adapter = RisApiAdapter(_profile(), client=RisApiClient(token="t", session=Session()))
    assert adapter.wait("42", poll_interval=1.0, sleep=lambda _s: None) == JobState.COMPLETED
