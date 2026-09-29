locals {
  name         = var.name
  account_id   = data.aws_caller_identity.current.account_id
  web_bucket   = "${var.name}-web-${local.account_id}"
  media_bucket = "${var.name}-media-${local.account_id}"
  csp = join("; ", [
    "default-src 'self'",
    "script-src 'self' https://cdn.jsdelivr.net",
    "style-src 'self'",
    "img-src 'self' blob: data: https://*.s3.${var.region}.amazonaws.com https://*.s3.amazonaws.com",
    "connect-src 'self' https://cognito-idp.${var.region}.amazonaws.com https://*.s3.${var.region}.amazonaws.com https://*.s3.amazonaws.com",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
  ])
}
