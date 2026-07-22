# TWAIN secrets (AWS Secrets Manager)

Terraform that manages a group of TWAIN secrets in AWS Secrets Manager, encrypted
with a dedicated KMS key, and readable only by a single IAM role that you assume.

## What it creates

| Resource | Purpose |
| --- | --- |
| `aws_secretsmanager_secret` (one per entry in `secrets.json`) | Named `TWAIN/<key>` and tagged `Project=TWAIN`, `Category=TWAIN` — the grouping convention |
| `aws_kms_key` + alias `alias/twain-secrets` | Customer-managed key encrypting every secret; only the read role may `Decrypt` |
| `aws_iam_role` `TWAIN-secrets-reader` | The only non-admin principal allowed to read/decrypt the secrets; its trust policy lets **you** assume it |

Access model: the KMS key policy grants `Decrypt` only to `TWAIN-secrets-reader`
(plus account administrators, who can always access resources in their account —
this is inherent to AWS and cannot be removed without locking yourself out). The
role's identity policy scopes `GetSecretValue`/`DescribeSecret` to exactly the
`TWAIN/*` secrets. So in practice: assume the role → read the secrets; nobody
else (short of an account admin) can.

## Secret definitions — `secrets.json` (never committed)

Secrets are defined in `secrets.json`, a **git-ignored** file. Start from the
template:

```bash
cp secrets.example.json secrets.json   # then fill in real values
```

Each entry is `"<name>": { "description": "...", "value": "..." }`. The `<name>`
may contain `/` to sub-group (e.g. `sso/azure-tenant-id` → secret
`TWAIN/sso/azure-tenant-id`). A bare string value also works:
`"my/name": "the-value"`.

`terraform.tfstate` also holds secret values in plaintext and is git-ignored too.
Do not share it; consider a remote encrypted backend (e.g. S3 + DynamoDB lock)
for team use.

## Usage

Prerequisites: Terraform ≥ 1.5, AWS credentials with permission to create KMS
keys, IAM roles, and Secrets Manager secrets (admin or equivalent).

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # optional; pin region / your ARN
cp secrets.example.json secrets.json           # fill in real values

terraform init
terraform plan
terraform apply
```

Pin the role to only you (recommended) by setting in `terraform.tfvars`:

```hcl
assume_role_principal_arns = ["arn:aws:iam::<account-id>:user/arifs"]
```

## Reading a secret (after apply)

```bash
# 1. Assume the role
creds=$(aws sts assume-role \
  --role-arn "$(terraform output -raw role_arn)" \
  --role-session-name twain-secrets)

export AWS_ACCESS_KEY_ID=$(echo "$creds"     | jq -r .Credentials.AccessKeyId)
export AWS_SECRET_ACCESS_KEY=$(echo "$creds" | jq -r .Credentials.SecretAccessKey)
export AWS_SESSION_TOKEN=$(echo "$creds"     | jq -r .Credentials.SessionToken)

# 2. Read a secret
aws secretsmanager get-secret-value \
  --secret-id TWAIN/sso/azure-client-id \
  --query SecretString --output text
```

## Changing secrets

Edit `secrets.json` and re-run `terraform apply`. Adding a key creates a new
secret; changing a `value` publishes a new version; removing a key deletes the
secret (subject to `recovery_window_in_days`).
