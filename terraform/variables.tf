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
