# WashU IT's WashU_IT_Deny_IAM_Specific denies iam:DeleteRolePolicy and
# iam:DetachRolePolicy on every role: Terraform here can add IAM grants but never
# remove one, so a change that replaces a role policy fails at its delete step.
# Grants therefore move by adding a new resource and *forgetting* the old one.
#
# These two first went to TWAIN-secrets-reader, which is not the API's task
# role; they now live on var.api_task_role_name (api_task_dispatch,
# api_task_run_data). The leftover copies on TWAIN-secrets-reader are harmless
# (that role only ever read secrets) -- deleting them needs WashU IT.
removed {
  from = aws_iam_role_policy.api_dispatch
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_iam_role_policy.api_run_data
  lifecycle {
    destroy = false
  }
}
