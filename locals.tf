locals {
  name         = var.name
  account_id   = data.aws_caller_identity.current.account_id
  web_bucket   = "${var.name}-web-${local.account_id}"
  media_bucket = "${var.name}-media-${local.account_id}"
  # A preview signs photo cookies with the key of the site it previews.
  media_key_from = var.media_key_from != null ? var.media_key_from : try(regex("^(.+)-pr[0-9]+$", var.name)[0], "")
  own_media_key  = local.media_key_from == ""
  # Exactly one of the lists has an element. A conditional would not do:
  # both of its sides are evaluated, and the one left out is empty.
  media_key = one(concat(
    [for index, group in aws_cloudfront_key_group.media : {
      key_group_id          = group.id
      public_key_id         = aws_cloudfront_public_key.media[index].id
      private_key_parameter = aws_ssm_parameter.media_signing_key[index].name
    }],
    # Ids and a parameter name; the data source marks every value sensitive.
    [for parameter in data.aws_ssm_parameter.shared_media_key : jsondecode(nonsensitive(parameter.value))],
  ))
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
