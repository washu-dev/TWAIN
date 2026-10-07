"""S3 staging for RIS API jobs (#170): transport, adapter flow, and the real job wrapper.

S3 is an in-memory fake; the job wrapper runs for real in bash against a local
stand-in for POST /api/job-tickets/urls that hands out file:// URLs.

Run from the repo root with:  pixi run pytest tests/unit/test_s3_staging.py
"""
import io
import json
import os
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from execution_adapter.cluster_profile import ClusterProfile
from execution_adapter.execution_result import ExecutionStatus
from execution_adapter.s3_transport import BUNDLE_KEY, OUTPUTS_KEY, S3Transport
from execution_adapter.slurm_execution_adapter import SlurmExecutionAdapter
from execution_adapter.staging import StagingError
from plan_synthesizer.execution_plan import SlurmRequest

REPO = Path(__file__).resolve().parents[2]
WRAPPER = REPO / "scripts" / "ris" / "job_wrapper.sh"


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError(f"NoSuchKey {Key}")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def _tar(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _names(blob: bytes) -> set:
    """Member files, as a run dir would see them (``./x`` -> ``x``; no ``.``)."""
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = {n[2:] if n.startswith("./") else n for n in tar.getnames()}
    return {n for n in names if n and n != "."}


# ── transport ──────────────────────────────────────────────────────────────────

def test_push_uploads_the_bundle_without_venv_or_secrets(tmp_path):
    (tmp_path / "main.py").write_text("print(1)")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("x")
    (tmp_path / ".twain_secrets.env").write_text("export MP_API_KEY=k")
    s3 = FakeS3()
    prefix = S3Transport("b", client=s3).push(tmp_path, "run1", 2)
    assert prefix == "runs/run1/attempt-2"
    assert _names(s3.objects[("b", f"{prefix}/{BUNDLE_KEY}")]) == {"main.py"}


def test_pull_unpacks_outputs_and_refuses_tar_slip(tmp_path):
    s3 = FakeS3()
    t = S3Transport("b", client=s3)
    s3.objects[("b", f"runs/r/attempt-1/{OUTPUTS_KEY}")] = _tar({"results.csv": b"gap,1.1\n"})
    t.pull("r", 1, tmp_path / "out")
    assert (tmp_path / "out" / "results.csv").read_text() == "gap,1.1\n"
    s3.objects[("b", f"runs/r/attempt-2/{OUTPUTS_KEY}")] = _tar({"../../escape.txt": b"x"})
    with pytest.raises(StagingError, match="outside the run dir"):
        t.pull("r", 2, tmp_path / "out2")
    assert not (tmp_path / "escape.txt").exists()


def test_missing_outputs_is_a_staging_error(tmp_path):
    with pytest.raises(StagingError, match="left no outputs"):
        S3Transport("b", client=FakeS3()).pull("r", 1, tmp_path)


# ── adapter flow ───────────────────────────────────────────────────────────────

def _profile():
    return ClusterProfile(
        name="compute2", login_nodes=["c2-login-001.ris.wustl.edu"],
        account="compute2-mdan", accounts=["compute2-mdan"], modules=["ris", "slurm"],
        default_partition="general-cpu", short_partition="general-short",
        storage_root="/storage2/fs1/mdan/Active/common/projects/twain",
        envs_root="/storage2/fs1/mdan/Active/common/projects/twain/twain-envs",
        partitions=[{"name": "general-cpu", "max_minutes": 21600, "gpus": False},
                    {"name": "general-short", "max_minutes": 30, "gpus": True}],
    )


class FakeRis:
    base_url = "https://d3n2m687w2hvtj.cloudfront.net/api/v1"

    def __init__(self, exit_code="0:0", stderr="", state="COMPLETED"):
        self.specs, self.exit_code, self.stderr_text, self.state = [], exit_code, stderr, state

    def submit_job(self, spec, *, idempotency_key=None):
        self.specs.append(spec)
        return str(100 + len(self.specs))

    def get_job(self, job_id):
        return {"job_id": job_id, "state": self.state}

    def accounting(self, job_id):
        return {"job_id": job_id, "state": self.state, "exit_code": self.exit_code, "elapsed": "60"}

    def stdout(self, job_id):
        return ""

    def output_tail(self, job_id, stream, nbytes):
        return self.stderr_text

    def output_page(self, job_id, stream, offset=0, limit=0):
        return {"content": "", "next_offset": 0, "size": 0}


def _bundle(tmp_path):
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "main.py").write_text("print('hi')")
    (b / "requirements.txt").write_text("ase\n")
    return b


def _adapter(tmp_path, ris, s3, tickets, **kw):
    def no_transfer(argv):
        raise AssertionError(f"S3 staging must not rsync/ssh: {argv}")
    return SlurmExecutionAdapter(
        _profile(), request=SlurmRequest(cpu_count=2, gpu_count=0, max_time=30, ram=4096),
        workspace_root=str(tmp_path), backend="api", ris_api_client=ris,
        staging="s3", s3_transport=S3Transport("twain-run-data", client=s3),
        issue_job_ticket=lambda *a: tickets.append(a) or "tkt-" + str(len(tickets)),
        api_public_url="https://d1z5umg4xc2bl8.cloudfront.net",
        env_file="/storage2/fs1/mdan/Active/common/projects/twain/TWAIN/twain.sh",
        expected_sha="abc1234", transfer_runner=no_transfer, cluster_runner=no_transfer,
        poll_interval=0.0, sleep=lambda _s: None, **kw)


def test_an_s3_run_never_touches_ssh_and_scopes_its_ticket(tmp_path):
    ris, s3, tickets = FakeRis(), FakeS3(), []
    adapter = _adapter(tmp_path, ris, s3, tickets)
    bundle = _bundle(tmp_path)
    run = adapter.execute(str(bundle), run_id="sess1")
    # no outputs were uploaded by the (fake) job -> a successful exit becomes FAILED
    assert run.status == ExecutionStatus.FAILED and "outputs are missing" in run.message

    (run_id, attempt, prefix, ttl), = tickets
    assert (run_id, attempt, prefix) == ("sess1", 1, "runs/sess1/attempt-1")
    assert 30 * 60 < ttl <= 7 * 86400
    script = ris.specs[0]["script"]
    assert "export TWAIN_TICKET=tkt-1" in script and "TWAIN_ATTEMPT=1" in script
    assert ". \"$TWAIN_ENV_FILE\"" in script and "TWAIN_STALE_CHECKOUT" in script
    assert 'exec bash "$CODE_DIR/scripts/ris/job_wrapper.sh"' in script
    assert "working_dir" not in ris.specs[0]          # the wrapper picks node scratch
    uploaded = _names(s3.objects[("twain-run-data", f"runs/sess1/attempt-1/{BUNDLE_KEY}")])
    assert {"main.py", "twain_payload.sh"} <= uploaded


def test_each_execute_is_a_new_attempt_with_its_own_ticket(tmp_path):
    ris, s3, tickets = FakeRis(), FakeS3(), []
    adapter = _adapter(tmp_path, ris, s3, tickets)
    bundle = _bundle(tmp_path)
    adapter.execute(str(bundle), run_id="s")
    adapter.execute(str(bundle), run_id="s")
    assert [t[2] for t in tickets] == ["runs/s/attempt-1", "runs/s/attempt-2"]


def test_outputs_are_pulled_and_the_run_succeeds(tmp_path):
    ris, s3, tickets = FakeRis(), FakeS3(), []
    adapter = _adapter(tmp_path, ris, s3, tickets)
    s3.objects[("twain-run-data", f"runs/s/attempt-1/{OUTPUTS_KEY}")] = _tar({"results.csv": b"x,1\n"})
    run = adapter.execute(str(_bundle(tmp_path)), run_id="s")
    assert run.status == ExecutionStatus.SUCCESS
    assert (Path(run.artifacts_dir) / "results.csv").read_text() == "x,1\n"


def test_a_stale_checkout_is_explained_not_reported_as_missing_outputs(tmp_path):
    ris = FakeRis(exit_code="4:0", state="FAILED", stderr=(
        "TWAIN_STALE_CHECKOUT: /x/twain is at 0d62459 and does not contain abc1234 "
        "(the code that submitted this job). Run: git -C /x/twain pull\n"))
    run = _adapter(tmp_path, ris, FakeS3(), []).execute(str(_bundle(tmp_path)), run_id="s")
    assert run.status == ExecutionStatus.SETUP_FAILED
    assert "RIS checkout is older" in run.message and "git -C /x/twain pull" in run.message


def test_a_broken_twain_sh_is_explained_as_setup(tmp_path):
    ris = FakeRis(exit_code="3:0", state="FAILED", stderr=(
        "TWAIN_ENV_FILE: CODE_DIR=<unset> is not a TWAIN checkout -- ...\n"))
    run = _adapter(tmp_path, ris, FakeS3(), []).execute(str(_bundle(tmp_path)), run_id="s")
    assert run.status == ExecutionStatus.SETUP_FAILED
    assert "twain.sh" in run.message


def _run_job_script(tmp_path, env_file_body=None, *, env_file=None):
    """Run the real job prologue in bash; returns (exit code, stderr)."""
    from execution_adapter.slurm_execution_adapter import _s3_job_script
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    if env_file_body is not None:
        env_file = tmp_path / "twain.sh"
        env_file.write_text(env_file_body)
    if env_file is not None:
        env["TWAIN_ENV_FILE"] = str(env_file)
    proc = subprocess.run(["bash", "-c", _s3_job_script()], env=env,
                          capture_output=True, text=True, timeout=30, check=False)
    return proc.returncode, proc.stderr


def test_job_script_names_a_missing_twain_sh(tmp_path):
    rc, err = _run_job_script(tmp_path, env_file=tmp_path / "nope" / "twain.sh")
    assert rc == 3 and "missing or not readable" in err and "nope/twain.sh" in err


def test_job_script_names_a_missing_code_dir(tmp_path):
    # the first live run: twain.sh defined only the old name, TWAIN_DIR
    rc, err = _run_job_script(tmp_path, f"export TWAIN_DIR={tmp_path}\n")
    assert rc == 3 and "CODE_DIR=<unset> is not a TWAIN checkout" in err


def test_job_script_runs_the_wrapper_from_code_dir(tmp_path):
    wrapper = tmp_path / "code" / "scripts" / "ris" / "job_wrapper.sh"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('echo "wrapper ran" >&2; exit 0\n')
    rc, err = _run_job_script(tmp_path, f"export CODE_DIR={tmp_path / 'code'}\n")
    assert rc == 0 and "wrapper ran" in err


def test_env_paths_honour_twain_envs_root_on_the_node(tmp_path):
    ris, s3 = FakeRis(), FakeS3()
    adapter = _adapter(tmp_path, ris, s3, [],
                       env_pythons=["/storage2/fs1/mdan/Active/common/projects/twain/twain-envs/psi4/bin/python"])
    adapter.execute(str(_bundle(tmp_path)), run_id="s")
    blob = s3.objects[("twain-run-data", f"runs/s/attempt-1/{BUNDLE_KEY}")]
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        payload = tar.extractfile("twain_payload.sh").read().decode()
    assert '"${TWAIN_ENVS_ROOT:-/storage2/fs1/mdan/Active/common/projects/twain/twain-envs}/psi4/bin/python"' in payload


@pytest.mark.parametrize("kw, match", [
    ({"backend": "ssh"}, "needs backend='api'"),
    ({"api_public_url": "http://insecure"}, "TWAIN_API_PUBLIC_URL"),
    ({"issue_job_ticket": None}, "issue_job_ticket"),
    ({"env_file": ""}, "TWAIN_ENV_FILE"),
])
def test_s3_staging_refuses_incomplete_configuration(tmp_path, monkeypatch, kw, match):
    monkeypatch.delenv("TWAIN_ENV_FILE", raising=False)
    base = dict(backend="api", staging="s3", ris_api_client=FakeRis(),
                s3_transport=S3Transport("b", client=FakeS3()),
                issue_job_ticket=lambda *a: "t", api_public_url="https://x.example",
                env_file="/x/twain.sh", workspace_root=str(tmp_path))
    base.update(kw)
    with pytest.raises(ValueError, match=match):
        SlurmExecutionAdapter(_profile(), **base)


# ── the real job wrapper, in bash ──────────────────────────────────────────────

class _TicketAPI(BaseHTTPRequestHandler):
    """Stands in for POST /api/job-tickets/urls, handing out file:// URLs."""
    store: Path = None
    calls: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append((self.headers.get("X-TWAIN-Ticket"), body))
        if self.headers.get("X-TWAIN-Ticket") != "good-ticket":
            self.send_response(401)
            self.end_headers()
            return
        urls = {o["name"]: (self.store / o["name"]).as_uri() for o in body["objects"]}
        out = json.dumps({"urls": urls, "expires_in": 3600}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def ticket_api(tmp_path):
    store = tmp_path / "s3"
    (store / "input").mkdir(parents=True)
    (store / "output").mkdir()
    _TicketAPI.store, _TicketAPI.calls = store, []
    server = HTTPServer(("127.0.0.1", 0), _TicketAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield store, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _run_wrapper(tmp_path, api_url, ticket="good-ticket"):
    env = dict(os.environ, TWAIN_API_URL=api_url, TWAIN_TICKET=ticket, TWAIN_RUN_ID="r1",
               TWAIN_ATTEMPT="1", TWAIN_SCRATCH=str(tmp_path / "scratch"), SLURM_JOB_ID="777")
    env.pop("TWAIN_ENVS_ROOT", None)
    return subprocess.run(["bash", str(WRAPPER)], env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(subprocess.run(["which", "curl"], capture_output=True).returncode != 0,
                    reason="curl not installed")
def test_the_wrapper_fetches_runs_and_uploads(tmp_path, ticket_api):
    store, url = ticket_api
    (store / "input" / "bundle.tar.gz").write_bytes(_tar({
        "twain_payload.sh": b"echo computing >&2\nprintf 'gap,1.12\\n' > results.csv\nexit 0\n",
        "main.py": b"print(1)\n"}))
    out = _run_wrapper(tmp_path, url)
    assert out.returncode == 0, out.stderr
    assert {"results.csv", "main.py", "twain_payload.sh"} <= _names((store / "output" / "outputs.tar.gz").read_bytes())
    assert [c[1]["objects"][0]["method"] for c in _TicketAPI.calls] == ["GET", "PUT"]


@pytest.mark.skipif(subprocess.run(["which", "curl"], capture_output=True).returncode != 0,
                    reason="curl not installed")
def test_the_wrapper_keeps_the_payload_exit_and_still_uploads(tmp_path, ticket_api):
    store, url = ticket_api
    (store / "input" / "bundle.tar.gz").write_bytes(_tar({
        "twain_payload.sh": b"echo 'MISSING DEPENDENCY: psi4' >&2\nexit 2\n"}))
    out = _run_wrapper(tmp_path, url)
    assert out.returncode == 2                         # dependency error, as classified upstream
    assert (store / "output" / "outputs.tar.gz").exists()   # logs come back anyway


@pytest.mark.skipif(subprocess.run(["which", "curl"], capture_output=True).returncode != 0,
                    reason="curl not installed")
def test_a_bad_ticket_is_exit_6(tmp_path, ticket_api):
    _store, url = ticket_api
    out = _run_wrapper(tmp_path, url, ticket="forged")
    assert out.returncode == 6 and "TWAIN_BUNDLE_FETCH_FAILED" in out.stderr
