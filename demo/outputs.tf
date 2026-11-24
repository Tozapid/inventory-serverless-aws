# The CI writes site_url into the summary of a deploy and the comment of a
# preview.
output "site_url" {
  description = "Address of the inventory."
  value       = module.inventory.site_url
}

output "cloudfront_url" {
  description = "CloudFront address of the inventory."
  value       = module.inventory.cloudfront_url
}

output "admin_username" {
  description = "Sign-in of the demo administrator."
  value       = module.inventory.admin_username
}
