# TWAIN infrastructure (Terraform)

Terraform for TWAIN's AWS pieces in account **730335203321**, region
**us-east-1**: secrets and their key, the run-data bucket, the job queues, the
IAM grants, and the worker's ECS service. It needs Terraform ≥ 1.7 (it uses
`removed` blocks) and AWS provider ~> 5.60.

The API service, ALB, CloudFront, the app bucket, ECR and RDS were created
before this Terraform existed and are **not** managed here (see the root
[README](../README.md#deploying), "Bootstrapping from scratch"). Diagram
[07](../docs/architecture/07_deployment_dependencies.drawio) shows how
everything connects.

> **WashU IT forbids IAM deletes.** `WashU_IT_Deny_IAM_Specific` denies
> `iam:DeleteRolePolicy`, `iam:DetachRolePolicy` and related actions on every
> role; `PutRolePolicy` is allowed. A plan that **replaces** or **destroys** an
> `aws_iam_role_policy` will fail half-way. Move a grant by adding a new
> resource address and *forgetting* the old one with
> `removed { lifecycle { destroy = false } }` (see `iam_add_only.tf`). Check
> every plan for "must be replaced" on IAM resources before applying.

## What it manages

| File | Resources | Purpose |
|---|---|---|
| `main.tf` | `aws_kms_key.twain_secrets` (+ alias), one `aws_secretsmanager_secret` + version per `secrets.json` entry (`TWAIN/<key>`) | All runtime secrets: DB password, LLM credentials, RIS API token and webhook secret, SSO identifiers, GitHub PAT, SendGrid key |
| `main.tf` | Role `TWAIN-secrets-reader` | Human or administrative read access to `TWAIN/*` |
| `main.tf` | Role `TWAIN-sso-ci-reader` | Lets the app build read only `sso/APP_ID` and `sso/TENANT_ID` |
| `main.tf` | `runner_execution_secrets` on `ecsTaskExecutionRole` | The worker's container secrets (`var.runner_secrets`), injected at start |
| `run_data.tf` | Bucket `twain-run-data-<account>` + policy, lifecycle, encryption | Slurm job files: `runs/<run>/attempt-<n>/{input,output}/…`; TLS only, owner-enforced, SSE-S3, `runs/` expires after 90 days |
| `run_data.tf` | `api_task_run_data` on `twain-api-ecs-task-role` | The API reads and writes `runs/*` in order to sign job and download links |
| `worker.tf` | `twain-jobs.fifo` (+ `twain-jobs-dlq.fifo`) | Job dispatch: visibility 900 s, redrive after 5 receives |
| `worker.tf` | `api_task_dispatch` on `twain-api-ecs-task-role` | The API sends job messages |
| `worker.tf` | Role `TWAIN-runner-worker` + policy | The worker: SQS consume/send, `runs/*` get/put, the DB password secret, KMS decrypt |
| `worker.tf` | Log group `/ecs/twain-runner`, bootstrap task definition, ECS service `twain-runner` | The worker service (Fargate, private subnets, no public IP). `ignore_changes` on the task definition, because CI deploys new revisions |
| `iam_add_only.tf` | `removed` blocks | Forget (never delete) the two policies first attached to the wrong role |

The execution role is `service-role/ecsTaskExecutionRole`. Look it up with
`data "aws_iam_role"`; never build its ARN by hand, because the path matters
(#177).

## Secrets: `secrets.json` (git-ignored)

```bash
cp secrets.example.json secrets.json      # then fill in values
```

Each entry is `"<name>": {"description": "...", "value": "..."}` and becomes
secret `TWAIN/<name>`. In use today:

| Key | Used by |
|---|---|
| `database/DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | API (all) and worker (password) |
| `secure_api/API_KEY`, `CLIENT_ID`, `CLIENT_SECRET` | Worker (LLM gateway) |
| `ris_api/TOKEN` · `ris_api/WEBHOOK_SECRET` | Worker (RIS API) · API (webhook signatures). A separate secret from RETICLE's, even if the PAT is the same |
| `sso/APP_ID`, `sso/TENANT_ID` (+ `sso/APP_SECRET`) | App build and API (Entra) |
| `github/GITHUB_ISSUE_TOKEN` | API (run reports, issues) |
| `sendgrid/API_KEY` | Worker (email, when `TWAIN_NOTIFY_BACKEND=sendgrid`) |

Removing a key deletes its secret, after `recovery_window_in_days` (7).
`terraform.tfstate` contains secret values in plain text: it is git-ignored, so
don't share it.

## Use

```bash
cd terraform
terraform init
terraform plan          # read it: no IAM replacements or destroys
terraform apply
terraform output        # role ARNs, bucket, queue URL, runner_secrets_missing (should be [])
```

Variables worth knowing (defaults in `variables.tf`):
- `api_task_role_name` = `twain-api-ecs-task-role`
- `ecs_execution_role_name` = `ecsTaskExecutionRole`
- `runner_secrets` (container env var → secret key)
- `run_data_retention_days` = 90
- `ecs_cluster_name` = `twain-cluster`
- `runner_desired_count` = 1
- `runner_subnet_ids`, `runner_security_group_ids`

To read a secret by hand, assume `TWAIN-secrets-reader`
(`terraform output -raw role_arn`) and run
`aws secretsmanager get-secret-value --secret-id TWAIN/<name>`.
