#!/usr/bin/env bash
#
# Wire the runner's secrets into runner/ecs-task-definition.json, replacing the
# `-REPLACE` placeholders with the ARNs of the Terraform-managed TWAIN/* secrets.
#
#   scripts/aws/setup_secrets.sh            # PLAN: show what it would do (no changes)
#   scripts/aws/setup_secrets.sh --apply    # patch the task def
#
# The secrets themselves live in Terraform (terraform/secrets.json, git-ignored):
# the LLM gateway creds under secure_api/*, the RIS API PAT under ris_api/TOKEN.
# `terraform apply` there creates them AND grants the ECS execution role read on
# exactly these ARNs; this script only copies the ARNs (from the
# `runner_secret_arns` output) into the task def. It reads no secret values.
#
# Requires: terraform, with `terraform apply` already run in terraform/.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="$REPO/terraform"
TASKDEF="$REPO/runner/ecs-task-definition.json"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

info() { printf '\033[1;36m%s\033[0m\n' "$1"; }
die()  { printf '\033[31merror: %s\033[0m\n' "$1" >&2; exit 1; }

command -v terraform >/dev/null 2>&1 || die "terraform not found on PATH"
[ "$APPLY" = 1 ] && info "MODE: APPLY (will patch the task def)" \
                 || info "MODE: PLAN (read-only; re-run with --apply to make changes)"

arns_json="$(terraform -chdir="$TF_DIR" output -json runner_secret_arns 2>/dev/null)" \
  || die "no runner_secret_arns output -- run 'terraform apply' in terraform/ first"
missing_json="$(terraform -chdir="$TF_DIR" output -json runner_secrets_missing 2>/dev/null || echo '[]')"

python3 - "$TASKDEF" "$APPLY" "$arns_json" "$missing_json" <<'PY'
import json, sys
path, apply, arns, missing = sys.argv[1], sys.argv[2] == "1", json.loads(sys.argv[3]), json.loads(sys.argv[4])
with open(path) as f:
    raw = f.read()
data = json.loads(raw)
wanted = [s["name"] for c in data.get("containerDefinitions", []) for s in c.get("secrets", [])]
unmapped = [n for n in wanted if n not in arns]
# Swap each valueFrom string in place rather than re-dumping the JSON, so the
# file's hand-aligned layout survives and the diff is one line per secret.
for c in data.get("containerDefinitions", []):
    for s in c.get("secrets", []):
        if s["name"] in arns:
            print(f"  {s['name']:14} -> {arns[s['name']]}")
            raw = raw.replace(json.dumps(s["valueFrom"]), json.dumps(arns[s["name"]]), 1)
if unmapped:
    hint = (f" (add {', '.join(missing)} to terraform/secrets.json -- see "
            f"secrets.example.json -- and re-apply)" if missing else "")
    sys.exit(f"error: no Terraform-managed secret for {', '.join(unmapped)}{hint}")
if apply:
    json.loads(raw)  # still valid JSON
    with open(path, "w") as f:
        f.write(raw)
PY

echo
if [ "$APPLY" = 1 ]; then
  info "Done. Commit runner/ecs-task-definition.json, then deploy the runner."
else
  echo "Re-run with --apply to patch runner/ecs-task-definition.json."
fi
