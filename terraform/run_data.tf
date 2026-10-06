# ─── Run data: the S3 file system of Slurm jobs submitted through the RIS API ───
# (#170) A run attempt's bundle goes up to runs/<run>/attempt-<n>/input/ and its
# outputs come back from .../output/. The cluster holds no AWS credentials: a job
# trades its ticket (api/job_tickets.py) for presigned URLs, which the API signs
# with the role granted below. Private, encrypted, TLS-only, and short-lived.

locals {
  run_bucket_name = var.run_bucket_name != "" ? var.run_bucket_name : "twain-run-data-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "run_data" {
  bucket = local.run_bucket_name
  tags   = merge(local.common_tags, { Name = local.run_bucket_name })
}

resource "aws_s3_bucket_public_access_block" "run_data" {
  bucket                  = aws_s3_bucket.run_data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "run_data" {
  bucket = aws_s3_bucket.run_data.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# SSE-S3 rather than the TWAIN KMS key: presigned PUTs from the cluster then
# need no KMS grant, and these are transient run artifacts, not secrets.
resource "aws_s3_bucket_server_side_encryption_configuration" "run_data" {
  bucket = aws_s3_bucket.run_data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "run_data" {
  bucket = aws_s3_bucket.run_data.id
  rule {
    id     = "expire-run-attempts"
    status = "Enabled"
    filter {
      prefix = "runs/"
    }
    expiration {
      days = var.run_data_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
}

data "aws_iam_policy_document" "run_data_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.run_data.arn,
      "${aws_s3_bucket.run_data.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "run_data" {
  bucket = aws_s3_bucket.run_data.id
  policy = data.aws_iam_policy_document.run_data_tls_only.json
  # The public-access block must exist first, or AWS may reject the policy.
  depends_on = [aws_s3_bucket_public_access_block.run_data]
}

# The API signs a job's URLs with its task role (TWAIN-secrets-reader, see
# api/ecs-task-definition.json), so that role needs the objects themselves.
data "aws_iam_policy_document" "api_run_data" {
  statement {
    sid       = "SignJobUrls"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.run_data.arn}/runs/*"]
  }
}

resource "aws_iam_role_policy" "api_run_data" {
  name   = "${var.name_prefix}-api-run-data"
  role   = aws_iam_role.twain_secrets.id
  policy = data.aws_iam_policy_document.api_run_data.json
}
