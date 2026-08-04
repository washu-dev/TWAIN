#!/usr/bin/env bash
#
# Create/update the runner's LLM-gateway secrets in AWS Secrets Manager from your
# local .env, then wire the resulting ARNs into runner/ecs-task-definition.json
# (replacing the `-REPLACE` placeholders). This removes the fiddliest manual
# deploy step.
#
#   scripts/aws/setup_secrets.sh            # PLAN: show what it would do (no changes)
#   scripts/aws/setup_secrets.sh --apply    # create/update secrets + patch the task def
#
# Requires: awscli v2 configured with credentials for account 730335203321, and
# the LLM creds present in the repo-root .env (API_KEY, CLIENT_ID, CLIENT_SECRET).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
TASKDEF="$REPO/runner/ecs-task-definition.json"
REGION="${AWS_REGION:-us-east-1}"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

# Stable secret names (no random suffix), so the ARNs are predictable + re-usable.
declare -A SECRET_NAME=(
  [API_KEY]="twain/llm/api-key"
  [CLIENT_ID]="twain/llm/client-id"
  [CLIENT_SECRET]="twain/llm/client-secret"
)

info() { printf '\033[1;36m%s\033[0m\n' "$1"; }
die()  { printf '\033[31merror: %s\033[0m\n' "$1" >&2; exit 1; }

command -v aws >/dev/null 2>&1 || die "awscli not found on PATH"
[ -f "$ENV_FILE" ] || die "no .env at $ENV_FILE (copy .env.example and fill the LLM creds)"

env_val() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//'; }

[ "$APPLY" = 1 ] && info "MODE: APPLY (will create/update secrets and patch the task def)" \
                 || info "MODE: PLAN (read-only; re-run with --apply to make changes)"
echo "Region: $REGION   Secret store: AWS Secrets Manager"
echo

declare -A RESULT_ARN
for var in API_KEY CLIENT_ID CLIENT_SECRET; do
  name="${SECRET_NAME[$var]}"
  value="$(env_val "$var")"
  [ -n "$value" ] || die "$var is empty in $ENV_FILE"

  if arn=$(aws secretsmanager describe-secret --secret-id "$name" --region "$REGION" \
             --query ARN --output text 2>/dev/null); then
    if [ "$APPLY" = 1 ]; then
      aws secretsmanager put-secret-value --secret-id "$name" \
        --secret-string "$value" --region "$REGION" >/dev/null
      echo "  updated  $name"
    else
      echo "  would update  $name (exists)"
    fi
  else
    if [ "$APPLY" = 1 ]; then
      arn=$(aws secretsmanager create-secret --name "$name" \
              --secret-string "$value" --region "$REGION" --query ARN --output text)
      echo "  created  $name"
    else
      echo "  would create  $name (new)"
      arn="(arn-assigned-on-create)"
    fi
  fi
  RESULT_ARN[$var]="$arn"
done

echo
if [ "$APPLY" = 1 ]; then
  info "Wiring ARNs into runner/ecs-task-definition.json"
  python3 - "$TASKDEF" "${RESULT_ARN[API_KEY]}" "${RESULT_ARN[CLIENT_ID]}" "${RESULT_ARN[CLIENT_SECRET]}" <<'PY'
import json, sys
path, api_key, client_id, client_secret = sys.argv[1:5]
arns = {"API_KEY": api_key, "CLIENT_ID": client_id, "CLIENT_SECRET": client_secret}
with open(path) as f:
    data = json.load(f)
patched = []
for c in data.get("containerDefinitions", []):
    for s in c.get("secrets", []):
        if s["name"] in arns:
            s["valueFrom"] = arns[s["name"]]
            patched.append(s["name"])
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print("  patched:", ", ".join(patched))
PY
  echo
  info "Done. Commit runner/ecs-task-definition.json, then deploy the runner."
else
  echo "Would set these valueFrom ARNs in runner/ecs-task-definition.json:"
  for var in API_KEY CLIENT_ID CLIENT_SECRET; do
    echo "  $var  ->  ${RESULT_ARN[$var]}"
  done
  echo
  echo "Re-run with --apply to create the secrets and patch the task def."
fi
