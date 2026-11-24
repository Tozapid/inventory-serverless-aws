# Production: the module on your own domain, invitations by mail, no public
# sign-in and no reset. Pin the module to a release tag; a newer
# release is a change of ref.
terraform {
  required_version = ">= 1.10.0"

  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "eu-central-1"
}

# CloudFront and its certificate live in us-east-1.
provider "aws" {
  alias  = "global"
  region = "us-east-1"
}

module "inventory" {
  source = "git::https://github.com/Tozapid/inventory-serverless-aws.git?ref=v1.8.1"

  providers = {
    aws        = aws
    aws.global = aws.global
  }

  name              = "inventory"
  domain_name       = "boxes.example.com"
  route53_zone_name = "example.com"
  # Cognito mails it a temporary password; it invites the others.
  admin_email = "you@example.com"
}

output "site_url" {
  description = "Address of the inventory."
  value       = module.inventory.site_url
}
