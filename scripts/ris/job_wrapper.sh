#!/usr/bin/env bash
# Runs ONE TWAIN bundle on a RIS compute node, with S3 as its file system (#170).
#
# Started by the job script the RIS API submits (rendered in
# modules/08_execution_adapter/ris_api_adapter.py), after it has sourced
# $TWAIN_ENV_FILE (twain.sh) and passed the stale-checkout guard. Needs only
# bash, curl, tar, and a python3 -- no AWS CLI, no boto3, no AWS credentials:
# the job's ticket buys short-lived presigned URLs from the TWAIN API.
#
#   in : TWAIN_API_URL TWAIN_TICKET TWAIN_RUN_ID TWAIN_ATTEMPT   (job environment)
#        TWAIN_HOME, TWAIN_SCRATCH (optional)                    (twain.sh)
#   do : GET bundle -> unpack -> bash twain_payload.sh (env pick, smoke test,
#        main.py) -> PUT outputs
#   out: the payload's exit code, or a TWAIN-specific one the runner explains:
#          6  could not fetch the bundle      7  could not upload the outputs
set -uo pipefail

: "${TWAIN_API_URL:?TWAIN_API_URL is not set}"
: "${TWAIN_TICKET:?TWAIN_TICKET is not set}"
: "${TWAIN_RUN_ID:?TWAIN_RUN_ID is not set}"
: "${TWAIN_ATTEMPT:=1}"

say() { echo "[twain-job] $*" >&2; }

# A python for JSON (compute nodes have no jq): the shared default env, else PATH.
PYJSON="${TWAIN_ENVS_ROOT:+$TWAIN_ENVS_ROOT/default/bin/python}"
[ -x "${PYJSON:-}" ] || PYJSON="$(command -v python3 || true)"
[ -n "$PYJSON" ] || { say "no python3 on this node to read the API's JSON"; exit 6; }

# Ask the API for presigned URLs; prints the URL for $1 (an object name).
url_for() {
  local name="$1" method="$2" body
  body="{\"objects\":[{\"name\":\"$name\",\"method\":\"$method\"}]}"
  curl -fsS --retry 4 --retry-delay 5 --retry-all-errors --max-time 60 \
       -X POST "$TWAIN_API_URL/api/job-tickets/urls" \
       -H "X-TWAIN-Ticket: $TWAIN_TICKET" -H "Content-Type: application/json" \
       -d "$body" \
    | "$PYJSON" -c "import json,sys; print(json.load(sys.stdin)['urls'][sys.argv[1]])" "$name"
}

base="${TWAIN_SCRATCH:-${TMPDIR:-/tmp}}"
work="$base/twain-$TWAIN_RUN_ID-a$TWAIN_ATTEMPT-${SLURM_JOB_ID:-$$}"
mkdir -p "$work" && cd "$work" || { say "cannot create $work"; exit 6; }
say "run $TWAIN_RUN_ID attempt $TWAIN_ATTEMPT on $(hostname) in $work"

# 1) bundle in
if ! url="$(url_for input/bundle.tar.gz GET)" \
   || ! curl -fsS --retry 4 --retry-delay 5 --retry-all-errors --max-time 600 -o bundle.tar.gz "$url" \
   || ! tar xzf bundle.tar.gz; then
  say "TWAIN_BUNDLE_FETCH_FAILED: could not download/unpack the bundle via $TWAIN_API_URL"
  exit 6
fi
rm -f bundle.tar.gz

# 2) run: env selection, the smoke test, then main.py (exit 2 = missing dependency)
bash twain_payload.sh
rc=$?
say "payload exited $rc"

# 3) outputs out -- always, so a failed run's logs and partial results come back too
# (COPYFILE_DISABLE: no macOS ._* metadata entries when this runs on a Mac in tests)
COPYFILE_DISABLE=1 tar czf "../outputs-$$.tar.gz" --exclude=./.venv --exclude=./__pycache__ . \
  && mv "../outputs-$$.tar.gz" outputs.tar.gz
if ! url="$(url_for output/outputs.tar.gz PUT)" \
   || ! curl -fsS --retry 4 --retry-delay 5 --retry-all-errors --max-time 600 -T outputs.tar.gz "$url"; then
  say "TWAIN_OUTPUT_UPLOAD_FAILED: the run finished (exit $rc) but its outputs could not be uploaded"
  [ "$rc" -eq 0 ] && exit 7
fi
exit "$rc"
