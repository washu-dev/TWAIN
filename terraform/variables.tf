variable "aws_region" {
  description = "AWS region in which to create the TWAIN secrets, KMS key, and IAM role."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Name prefix used to group all secrets (e.g. TWAIN/<name>). Also used as the KMS alias/role-name stem."
  type        = string
  default     = "TWAIN"
}

variable "role_name" {
  description = "Name of the IAM role granted read access to the TWAIN secrets."
  type        = string
  default     = "TWAIN-secrets-reader"
}

variable "secrets_file" {
  description = "Path to the git-ignored JSON file defining the secrets. Defaults to ./secrets.json in this module."
  type        = string
  default     = ""
}

variable "assume_role_principal_arns" {
  description = <<-EOT
    IAM principal ARNs allowed to assume the read role ("assigned to me").
    Leave empty to default to the identity running Terraform. Prefer setting
    your stable IAM user/role ARN, e.g. ["arn:aws:iam::<acct>:user/arifs"].
  EOT
  type        = list(string)
  default     = []
}

variable "ci_principal_arns" {
  description = "IAM principal ARNs (e.g. the CI user) allowed to assume the SSO-only CI reader role. Empty disables the role."
  type        = list(string)
  default     = []
}

variable "ci_role_name" {
  description = "Name of the SSO-scoped CI reader role used to inject EXPO_PUBLIC_AZURE_* at build time."
  type        = string
  default     = "TWAIN-sso-ci-reader"
}

variable "sso_ci_secret_keys" {
  description = "secrets.json keys the CI reader role may read (the public SSO identifiers injected into the web build)."
  type        = list(string)
  default     = ["sso/APP_ID", "sso/TENANT_ID"]
}

variable "trusted_service_principals" {
  description = "AWS service principals allowed to assume the read role (e.g. ECS tasks that read the secrets at runtime)."
  type        = list(string)
  default     = ["ecs-tasks.amazonaws.com"]
}

variable "recovery_window_in_days" {
  description = "Days AWS retains a deleted secret before permanent deletion (0 = delete immediately)."
  type        = number
  default     = 7
}

variable "tags" {
  description = "Additional tags merged onto every resource."
  type        = map(string)
  default     = {}
}

variable "ecs_execution_role_name" {
  description = "Existing ECS task execution role that injects the runner's `secrets` (runner/ecs-task-definition.json executionRoleArn). Granted read on var.runner_secrets only. Empty string = don't grant."
  type        = string
  default     = "ecsTaskExecutionRole"
}

variable "runner_secrets" {
  description = "Runner container env var => secrets.json key, for the `secrets` block of runner/ecs-task-definition.json (wired by scripts/aws/setup_secrets.sh)."
  type        = map(string)
  default = {
    API_KEY       = "secure_api/API_KEY"
    CLIENT_ID     = "secure_api/CLIENT_ID"
    CLIENT_SECRET = "secure_api/CLIENT_SECRET"
    RIS_API_TOKEN = "ris_api/TOKEN"
  }
}

variable "run_bucket_name" {
  description = "S3 bucket for Slurm job file I/O (bundles in, outputs out). Empty = twain-run-data-<account id>."
  type        = string
  default     = ""
}

variable "run_data_retention_days" {
  description = "Days a run attempt's files stay in the run bucket before expiring."
  type        = number
  default     = 90
}
