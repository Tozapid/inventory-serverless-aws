variable "region" {
  type        = string
  default     = "eu-central-1"
  description = "Region for the API, data and identity resources."
}

variable "name" {
  type        = string
  default     = "inventory-serverless"
  description = "Prefix of every resource name. Bucket names add the account id."
}

variable "demo" {
  type        = bool
  default     = true
  description = "Public demo: the sign-in screen shows and fills in the admin sign-in. Turn off for a private inventory."
}

variable "admin_email" {
  type        = string
  default     = "admin@example.com"
  description = "Sign-in of the first administrator. Self-registration is disabled."
}

variable "admin_password" {
  type        = string
  default     = "admin123"
  sensitive   = true
  description = "Password of the first administrator. In the demo the hourly reset puts it back."
}

variable "password_min_length" {
  type        = number
  default     = 8
  description = "Minimum password length. The pool also asks for a lower case letter and a digit."
}

variable "invite_emails" {
  type        = bool
  default     = false
  description = "Send invitation emails. Off for a public demo, so it cannot be used to send mail to strangers."
}

variable "recognition" {
  type        = bool
  default     = false
  description = "Photo recognition with the user's own Grok API key. Off in the public demo: everyone signs in as the same admin and would see the key."
}

variable "photo_max_bytes" {
  type        = number
  default     = 6291456
  description = "Maximum stored photo size. The browser keeps the long side within 2560 pixels."
}

variable "url_ttl_seconds" {
  type        = number
  default     = 900
  description = "Lifetime of the signed upload forms returned by the API."
}

variable "reset_schedule" {
  type        = string
  default     = "rate(1 hour)"
  description = "When the demo is wiped back to its initial state. An empty string turns the reset off."
}

variable "access_logs_bucket" {
  type        = string
  default     = ""
  description = "Bucket that the access logs of this site go to, such as the logs_bucket of an overview of the account (Tozapid/account-serverless-aws): the standard logs (v2) of the distribution under cloudfront/<name>/. Its policy has to let delivery.logs.amazonaws.com write there. Empty: no access logs."
}

variable "tags" {
  type = map(string)
  default = {
    Managed = "IAC"
    Project = "inventory-serverless-aws"
  }
  description = "Tags applied to supported resources."
}

variable "releases_repository" {
  type        = string
  default     = "Tozapid/inventory-serverless-aws"
  description = "GitHub repository (owner/name) whose releases the settings compare the version of the site with. Empty turns the check off."
}

variable "media_key_from" {
  type        = string
  default     = null
  description = "Name of a deployed stack of this project whose CloudFront key signs the photo cookies, instead of a key of its own; \"\" for its own. Unset: a preview (<name>-pr<N>) uses <name>'s, any other stack its own. The account allows ten CloudFront public keys."
}
