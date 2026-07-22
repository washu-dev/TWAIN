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
