terraform {
  required_version = ">= 1.7" # removed blocks (iam_add_only.tf)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}
