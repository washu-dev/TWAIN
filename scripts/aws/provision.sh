#!/usr/bin/env bash
#
# Idempotent provisioning of the TWAIN deploy targets on AWS. Creates the
# resources the GitHub deploy workflows assume already exist, then registers the
# ECS task definitions. Safe to re-run: every step checks-then-creates.
#
#   scripts/aws/provision.sh            # PLAN: print what it would do (no changes)
#   scripts/aws/provision.sh --apply    # create ECR repos, log groups, cluster,
#                                        # and register both task definitions
#
# It does NOT create the two ECS *services* by default: those need your VPC
# subnets + security group (and, for the API, an ALB target group), which vary
# per account. It prints exact `aws ecs create-service` commands for them; set
# SUBNETS / SECURITY_GROUPS (and API_TARGET_GROUP_ARN) and pass --apply to run
# the runner service create too.
#
# Prereqs already present in this account (per the task definitions): RDS
# `twaindb`, the DB-password secret, the S3 bucket + CloudFront for the app, and
# the ecsTaskExecutionRole / ecsTaskRole IAM roles.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="${AWS_REGION:-us-east-1}"
CLUSTER="twain-cluster"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

ECR_REPOS=(twain-ecr twain-runner-ecr)
LOG_GROUPS=(/ecs/twain-api /ecs/twain-runner)

info() { printf '\033[1;36m%s\033[0m\n' "$1"; }
step() { printf '  %s\n' "$1"; }
die()  { printf '\033[31merror: %s\033[0m\n' "$1" >&2; exit 1; }
run()  { if [ "$APPLY" = 1 ]; then "$@"; else echo "    would run: $*"; fi; }

command -v aws >/dev/null 2>&1 || die "awscli not found on PATH"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
  || die "no usable AWS credentials (configure the awscli first)"

[ "$APPLY" = 1 ] && info "MODE: APPLY" || info "MODE: PLAN (read-only; re-run with --apply)"
echo "Account: $ACCOUNT   Region: $REGION   Cluster: $CLUSTER"

# ── ECR repositories ──────────────────────────────────────────────────────────
info "ECR repositories"
# Storage cap: every deploy pushes a new image (the runner's is multi-GB) and
# ECR bills per GB-month; keep only the 5 newest per repo.
LIFECYCLE_POLICY='{"rules":[{"rulePriority":1,"description":"Keep only the 5 newest images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}'
for repo in "${ECR_REPOS[@]}"; do
  if aws ecr describe-repositories --repository-names "$repo" --region "$REGION" >/dev/null 2>&1; then
    step "$repo — exists"
  else
    step "$repo — creating"
    run aws ecr create-repository --repository-name "$repo" --region "$REGION" \
        --image-scanning-configuration scanOnPush=true >/dev/null
  fi
  step "$repo — lifecycle policy (keep 5 newest images)"
  run aws ecr put-lifecycle-policy --repository-name "$repo" --region "$REGION" \
      --lifecycle-policy-text "$LIFECYCLE_POLICY" >/dev/null
done

# ── CloudWatch log groups ──────────────────────────────────────────────────────
info "CloudWatch log groups"
for group in "${LOG_GROUPS[@]}"; do
  existing="$(aws logs describe-log-groups --log-group-name-prefix "$group" --region "$REGION" \
                --query "logGroups[?logGroupName=='$group'].logGroupName" --output text 2>/dev/null || true)"
  if [ -n "$existing" ]; then
    step "$group — exists"
  else
    step "$group — creating"
    run aws logs create-log-group --log-group-name "$group" --region "$REGION"
  fi
done

# ── ECS cluster ────────────────────────────────────────────────────────────────
info "ECS cluster"
status="$(aws ecs describe-clusters --clusters "$CLUSTER" --region "$REGION" \
            --query "clusters[0].status" --output text 2>/dev/null || true)"
if [ "$status" = "ACTIVE" ]; then
  step "$CLUSTER — active"
else
  step "$CLUSTER — creating"
  run aws ecs create-cluster --cluster-name "$CLUSTER" --region "$REGION" >/dev/null
fi

# ── Task definitions ───────────────────────────────────────────────────────────
info "Registering task definitions"
for td in api/ecs-task-definition.json runner/ecs-task-definition.json; do
  if grep -q -- "-REPLACE" "$REPO/$td"; then
    step "$td — SKIPPED: contains -REPLACE placeholders (run scripts/aws/setup_secrets.sh --apply first)"
    continue
  fi
  step "$td — register-task-definition"
  run aws ecs register-task-definition --cli-input-json "file://$REPO/$td" \
      --region "$REGION" >/dev/null
done

# ── ECS services (networking-specific; opt-in) ─────────────────────────────────
info "ECS services"
SUBNETS="${SUBNETS:-}"
SECURITY_GROUPS="${SECURITY_GROUPS:-}"
API_TARGET_GROUP_ARN="${API_TARGET_GROUP_ARN:-}"

runner_exists="$(aws ecs describe-services --cluster "$CLUSTER" --services twain-runner \
  --region "$REGION" --query "services[?status=='ACTIVE'].serviceName" --output text 2>/dev/null || true)"
api_exists="$(aws ecs describe-services --cluster "$CLUSTER" --services twain-api \
  --region "$REGION" --query "services[?status=='ACTIVE'].serviceName" --output text 2>/dev/null || true)"

net_cfg() { echo "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SECURITY_GROUPS],assignPublicIp=ENABLED}"; }

# Runner: no inbound (it polls the jobs table), so no load balancer needed.
if [ -n "$runner_exists" ]; then
  step "twain-runner — exists"
elif [ -n "$SUBNETS" ] && [ -n "$SECURITY_GROUPS" ]; then
  step "twain-runner — creating"
  run aws ecs create-service --cluster "$CLUSTER" --service-name twain-runner \
      --task-definition twain-runner --desired-count 1 --launch-type FARGATE \
      --network-configuration "$(net_cfg)" --region "$REGION" >/dev/null
else
  step "twain-runner — MISSING. Set SUBNETS + SECURITY_GROUPS and re-run --apply, or run:"
  cat <<EOF
      aws ecs create-service --cluster $CLUSTER --service-name twain-runner \\
        --task-definition twain-runner --desired-count 1 --launch-type FARGATE \\
        --network-configuration 'awsvpcConfiguration={subnets=[subnet-…],securityGroups=[sg-…],assignPublicIp=ENABLED}' \\
        --region $REGION
EOF
fi

# API: needs inbound HTTPS, so it must be behind the ALB target group.
if [ -n "$api_exists" ]; then
  step "twain-api — exists"
else
  step "twain-api — MISSING. It must register with the ALB target group. Run:"
  cat <<EOF
      aws ecs create-service --cluster $CLUSTER --service-name twain-api \\
        --task-definition twain-api --desired-count 1 --launch-type FARGATE \\
        --network-configuration 'awsvpcConfiguration={subnets=[subnet-…],securityGroups=[sg-…],assignPublicIp=ENABLED}' \\
        --load-balancers 'targetGroupArn=${API_TARGET_GROUP_ARN:-arn:aws:elasticloadbalancing:…:targetgroup/…},containerName=twain-api,containerPort=8000' \\
        --health-check-grace-period-seconds 60 --region $REGION
EOF
fi

echo
[ "$APPLY" = 1 ] && info "Provisioning pass complete." \
                 || info "PLAN only — nothing changed. Re-run with --apply."
echo "Next: verify with  python scripts/preflight.py --aws"
