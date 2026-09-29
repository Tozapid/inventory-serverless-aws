terraform {
  required_version = ">= 1.10.0"

  # Partial configuration: bucket, key and region come from terraform init,
  # see README. The S3 lockfile replaces a DynamoDB lock table.
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.6"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}
