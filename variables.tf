variable "name" {
  type        = string
  description = "Prefix of every resource name, such as inventory; bucket names add the account id."
}

variable "demo" {
  type        = bool
  default     = false
  description = "Public demo: the sign-in screen shows and fills in the admin sign-in, a destroy takes the photos along and no invitation is mailed."
}

variable "domain_name" {
  type        = string
  default     = ""
  description = "Name of the inventory, such as boxes.example.com, in the Route 53 zone route53_zone_name. Empty: the inventory answers at its CloudFront address (the demo)."
}

variable "route53_zone_name" {
  type        = string
  default     = ""
  description = "Public Route 53 zone of domain_name; the module writes the certificate's validation and the alias records there."
}

variable "admin_email" {
  type        = string
  description = "Sign-in of the first user, an administrator. Self-registration is disabled; administrators invite the others in the settings."
}

variable "admin_password" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Permanent password of the first user, at least 12 characters with upper and lower case letters, a digit and a symbol; the demo publishes it and the reset puts it back. Empty: Cognito mails admin_email a temporary password instead."
}

variable "invite_emails" {
  type        = bool
  default     = true
  description = "Mail the invitation with the temporary password to a new user. The demo never mails, so it cannot be used to send mail to strangers: the administrator passes the password on."
}

variable "recognition" {
  type        = bool
  default     = true
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
  default     = ""
  description = "When the demo is wiped back to its initial state: the boxes of seed/boxes.json come back, the administrator gets admin_password back and other users go. An empty string, the default, turns the reset off."
}

variable "access_logs_bucket" {
  type        = string
  default     = ""
  description = "Bucket that the access logs of this site go to, such as the logs_bucket of an overview of the account (Tozapid/account-serverless-aws): the standard logs (v2) of the distribution under cloudfront/<name>/. Its policy has to let delivery.logs.amazonaws.com write there. Empty: no access logs."
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
