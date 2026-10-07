output "role_arn" {
  description = "ARN of the IAM role that can read the TWAIN secrets. Assume this role to fetch them."
  value       = aws_iam_role.twain_secrets.arn
}

output "kms_key_arn" {
  description = "ARN of the customer-managed KMS key encrypting the TWAIN secrets."
  value       = aws_kms_key.twain_secrets.arn
}

output "secret_arns" {
  description = "Map of secret name => ARN for every managed TWAIN secret."
  value       = { for k, s in aws_secretsmanager_secret.this : s.name => s.arn }
}

output "sso_ci_reader_role_arn" {
  description = "ARN of the SSO-only CI reader role (null if not created). Set this as TWAIN_SSO_CI_ROLE_ARN in CI."
  value       = local.create_ci_role ? aws_iam_role.sso_ci_reader[0].arn : null
}

output "runner_secret_arns" {
  description = "Runner env var => secret ARN, for runner/ecs-task-definition.json (scripts/aws/setup_secrets.sh wires these)."
  value       = local.runner_secret_arns
}

output "runner_secrets_missing" {
  description = "secrets.json keys the runner needs that don't exist yet (add them, then re-apply)."
  value       = local.runner_secrets_missing
}

output "run_bucket_name" {
  description = "S3 bucket for Slurm job file I/O; set as TWAIN_RUN_BUCKET for the API and the runner/worker."
  value       = aws_s3_bucket.run_data.bucket
}

output "jobs_queue_url" {
  description = "SQS FIFO job queue; set as TWAIN_JOB_QUEUE_URL for the API and the worker."
  value       = aws_sqs_queue.jobs.url
}

output "runner_worker_role_arn" {
  description = "Task role of the twain-runner worker service."
  value       = aws_iam_role.runner_worker.arn
}
