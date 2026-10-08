# ─── Event-driven runner (P2, #171): job queue + the twain-runner worker service ─
# The API inserts a job row ('dispatching') and sends its id to this FIFO queue;
# workers (runner/worker.py on ECS Fargate) claim and drive it. EXECUTE submits to
# RIS and pauses; the worker's cluster monitor resumes the run when the Slurm job
# finishes. Activation is two-step and safe by default: this creates an idle
# worker; flipping the repo variable TWAIN_DISPATCH to "sqs" (deploy-api.yml) is
# what makes the API start dispatching here.

locals {
  jobs_queue_name = "${lower(var.name_prefix)}-jobs.fifo"
}

resource "aws_sqs_queue" "jobs_dlq" {
  name                      = "${lower(var.name_prefix)}-jobs-dlq.fifo"
  fifo_queue                = true
  message_retention_seconds = 1209600 # 14 days to inspect what kept failing
  sqs_managed_sse_enabled   = true
  tags                      = local.common_tags
}

resource "aws_sqs_queue" "jobs" {
  name       = local.jobs_queue_name
  fifo_queue = true
  # Dedup ids are explicit ("job-<id>"), not body hashes: a deliberate re-send
  # of the same job by the relay must still be dropped within 5 minutes.
  content_based_deduplication = false
  # The worker extends this while a job runs (runner/worker.py).
  visibility_timeout_seconds = 900
  message_retention_seconds  = 345600 # 4 days
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 5
  })
  tags = local.common_tags
}

# The API sends job messages (api/dispatch.py) with its task role.
# (Without this the send is denied and every job waits for the relay.)
data "aws_iam_policy_document" "api_dispatch" {
  statement {
    sid       = "SendJobs"
    effect    = "Allow"
    actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

resource "aws_iam_role_policy" "api_task_dispatch" {
  name   = "${var.name_prefix}-api-dispatch"
  role   = var.api_task_role_name
  policy = data.aws_iam_policy_document.api_dispatch.json
}

# ── The worker's task role: what runner/worker.py itself calls ────────────────
# (Secrets injected at start -- LLM creds, RIS token -- come through the
# execution role, see runner_execution_secrets in main.tf.)
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "runner_worker" {
  name               = "${var.name_prefix}-runner-worker"
  description        = "TWAIN worker (runner/worker.py): job queue, run bucket, DB secret"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "runner_worker" {
  statement {
    sid    = "ConsumeAndRequeueJobs"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
      "sqs:SendMessage", "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }
  statement {
    sid       = "RunFiles"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.run_data.arn}/runs/*"]
  }
  statement {
    sid       = "DbPassword"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.this["database/DB_PASSWORD"].arn]
  }
  statement {
    sid       = "DecryptWithTwainKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.twain_secrets.arn]
  }
}

resource "aws_iam_role_policy" "runner_worker" {
  name   = "${var.name_prefix}-runner-worker"
  role   = aws_iam_role.runner_worker.id
  policy = data.aws_iam_policy_document.runner_worker.json
}

# ── The service ───────────────────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "runner" {
  name              = "/ecs/twain-runner"
  retention_in_days = 30
  tags              = local.common_tags
}

# Looked up, not assembled: the role sits under a path, and an ARN without it
# names no role -- ECS then fails every launch with "unable to assume the role".
data "aws_iam_role" "ecs_execution" {
  name = var.ecs_execution_role_name
}

# Bootstrap revision only: CI (ci-runner.yml) registers every later revision from
# runner/ecs-task-definition.json, so the service ignores task_definition drift.
resource "aws_ecs_task_definition" "runner_bootstrap" {
  family                   = "twain-runner"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = jsondecode(file("${path.module}/../runner/ecs-task-definition.json")).cpu
  memory                   = jsondecode(file("${path.module}/../runner/ecs-task-definition.json")).memory
  execution_role_arn       = data.aws_iam_role.ecs_execution.arn # carries the role's path (service-role/)
  task_role_arn            = aws_iam_role.runner_worker.arn
  container_definitions    = jsonencode(jsondecode(file("${path.module}/../runner/ecs-task-definition.json")).containerDefinitions)
  tags                     = local.common_tags
}

resource "aws_ecs_service" "runner" {
  name            = "twain-runner"
  cluster         = var.ecs_cluster_name
  task_definition = aws_ecs_task_definition.runner_bootstrap.arn
  desired_count   = var.runner_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.runner_subnet_ids
    security_groups  = var.runner_security_group_ids
    assign_public_ip = false
  }

  # Start the new task before stopping the old one. Overlap is safe: claims
  # are idempotent per job, and the monitor's advisory lock keeps one leader.
  # 100 (not 0): with 0, ECS stopped the working worker before a replacement
  # had started, so a broken revision (2026-10-08) left nothing running.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # A revision whose tasks can't start (a bad secret reference, a broken image)
  # is rolled back by ECS, and the CI deploy fails loudly instead of hanging.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [task_definition] # CI owns revisions
  }
  tags = local.common_tags
}
