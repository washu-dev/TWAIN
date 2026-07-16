provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  secrets_file = var.secrets_file != "" ? var.secrets_file : "${path.module}/secrets.json"

  # Map of "<name>" => { description = "...", value = "..." } (value may also be a bare string).
  secrets = jsondecode(file(local.secrets_file))

  common_tags = merge(
    {
      Project   = "TWAIN"
      Category  = "TWAIN"
      ManagedBy = "terraform"
    },
    var.tags,
  )

  # Principals allowed to assume the read role. Defaults to whoever runs Terraform.
  assume_principals = length(var.assume_role_principal_arns) > 0 ? var.assume_role_principal_arns : [data.aws_caller_identity.current.arn]

  # Constructed (not resource-derived) role ARN so the KMS key policy can grant
  # the role decrypt without creating a key <-> role dependency cycle.
  role_arn = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${var.role_name}"

  account_root_arn = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"
}

# ─── KMS customer-managed key: encrypts every TWAIN secret ─────────────────────

data "aws_iam_policy_document" "kms" {
  # Prevent lockout: the account retains administrative control (AWS best practice).
  statement {
    sid       = "EnableAccountAdmin"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [local.account_root_arn]
    }
  }

  # Only the TWAIN read role may decrypt secret values with this key.
  statement {
    sid       = "AllowTwainRoleDecrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [local.role_arn]
    }
  }
}

resource "aws_kms_key" "twain_secrets" {
  description             = "Encrypts ${var.name_prefix} Secrets Manager secrets"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.kms.json
  tags                    = local.common_tags
}

resource "aws_kms_alias" "twain_secrets" {
  name          = "alias/${lower(var.name_prefix)}-secrets"
  target_key_id = aws_kms_key.twain_secrets.key_id
}

# ─── Secrets Manager: one secret per entry in secrets.json, grouped by prefix ──

resource "aws_secretsmanager_secret" "this" {
  for_each = local.secrets

  name                    = "${var.name_prefix}/${each.key}"
  description             = try(each.value.description, null)
  kms_key_id              = aws_kms_key.twain_secrets.arn
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(local.common_tags, { Name = "${var.name_prefix}/${each.key}" })
}

resource "aws_secretsmanager_secret_version" "this" {
  for_each = local.secrets

  secret_id     = aws_secretsmanager_secret.this[each.key].id
  secret_string = try(tostring(each.value.value), tostring(each.value))
}

# ─── IAM role: the only non-admin principal that can read the secrets ──────────

data "aws_iam_policy_document" "assume" {
  statement {
    sid     = "AllowAssignedPrincipalsToAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = local.assume_principals
    }
  }

  # Lets AWS services (e.g. ECS tasks) assume the role at runtime so workloads
  # can read the TWAIN secrets without long-lived credentials.
  dynamic "statement" {
    for_each = length(var.trusted_service_principals) > 0 ? [1] : []
    content {
      sid     = "AllowServicesToAssume"
      effect  = "Allow"
      actions = ["sts:AssumeRole"]
      principals {
        type        = "Service"
        identifiers = var.trusted_service_principals
      }
    }
  }
}

resource "aws_iam_role" "twain_secrets" {
  name               = var.role_name
  description        = "Read-only access to ${var.name_prefix} Secrets Manager secrets"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "secrets_read" {
  statement {
    sid    = "ReadTwainSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = [for s in aws_secretsmanager_secret.this : s.arn]
  }

  # ListSecrets cannot be scoped to a resource in IAM, so this lets the role
  # enumerate the NAMES (metadata) of ALL secrets in the account, not just
  # TWAIN/*. It can still only read the VALUES of TWAIN/* (see ReadTwainSecrets
  # above) and only decrypt with the TWAIN key. Remove this statement entirely
  # if even account-wide name visibility is undesirable.
  statement {
    sid       = "ListAllSecrets"
    effect    = "Allow"
    actions   = ["secretsmanager:ListSecrets"]
    resources = ["*"]
  }

  statement {
    sid       = "DecryptWithTwainKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.twain_secrets.arn]
  }
}

resource "aws_iam_role_policy" "twain_secrets_read" {
  name   = "${var.role_name}-read"
  role   = aws_iam_role.twain_secrets.id
  policy = data.aws_iam_policy_document.secrets_read.json
}
