resource "tls_private_key" "media" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "aws_cloudfront_public_key" "media" {
  provider    = aws.global
  name        = "${local.name}-media"
  encoded_key = tls_private_key.media.public_key_pem
}

resource "aws_cloudfront_key_group" "media" {
  provider = aws.global
  name     = "${local.name}-media"
  items    = [aws_cloudfront_public_key.media.id]
}

resource "aws_ssm_parameter" "media_signing_key" {
  #checkov:skip=CKV_AWS_337:The AWS managed SSM key is enough for this CloudFront signing key.
  name        = "/${local.name}/cloudfront-private-key"
  description = "Private key used to sign CloudFront cookies for inventory photos."
  type        = "SecureString"
  value       = tls_private_key.media.private_key_pem
}

resource "aws_cloudfront_function" "api_host" {
  provider = aws.global
  name     = "${local.name}-api-host"
  runtime  = "cloudfront-js-2.0"
  publish  = true
  comment  = "Remember the viewer host so photo cookies match the site address."
  code     = file("${path.module}/cloudfront/api_host.js")
}

resource "aws_cloudfront_function" "media_path" {
  provider = aws.global
  name     = "${local.name}-media-path"
  runtime  = "cloudfront-js-2.0"
  publish  = true
  comment  = "Strip /media before reading the private photo bucket."
  code     = file("${path.module}/cloudfront/media.js")
}

resource "aws_cloudfront_cache_policy" "media" {
  provider    = aws.global
  name        = "${local.name}-media"
  comment     = "Cache photos by path. The signed cookie is checked on every request and is not part of the cache key."
  default_ttl = 86400
  max_ttl     = 31536000
  min_ttl     = 0

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true

    headers_config {
      header_behavior = "none"
    }

    cookies_config {
      cookie_behavior = "none"
    }

    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

resource "aws_cloudfront_response_headers_policy" "media" {
  provider = aws.global
  name     = "${local.name}-media"

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
      preload                    = true
    }
  }

  custom_headers_config {
    items {
      header   = "Cache-Control"
      override = true
      value    = "private, max-age=86400"
    }
  }
}

resource "aws_cloudfront_origin_access_control" "web" {
  provider                          = aws.global
  name                              = local.web_bucket
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_cache_policy" "web" {
  provider    = aws.global
  name        = "${local.name}-web"
  comment     = "Short cache for the inventory shell. API responses are not cached."
  default_ttl = 0
  max_ttl     = 86400
  min_ttl     = 0

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true

    headers_config {
      header_behavior = "none"
    }

    cookies_config {
      cookie_behavior = "none"
    }

    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

resource "aws_cloudfront_response_headers_policy" "web" {
  provider = aws.global
  name     = "${local.name}-web"

  security_headers_config {
    content_type_options {
      override = true
    }

    frame_options {
      frame_option = "DENY"
      override     = true
    }

    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
      preload                    = true
    }

    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }

    content_security_policy {
      content_security_policy = local.csp
      override                = true
    }
  }
}

data "aws_cloudfront_cache_policy" "caching_disabled" {
  provider = aws.global
  name     = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  provider = aws.global
  name     = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_function" "routes" {
  provider = aws.global
  name     = "${local.name}-routes"
  runtime  = "cloudfront-js-2.0"
  publish  = true
  comment  = "Serve the inventory shell for direct card links."
  code     = file("${path.module}/cloudfront/routes.js")
}

resource "aws_cloudfront_distribution" "web" {
  #checkov:skip=CKV_AWS_374: "Ensure AWS CloudFront web distribution has geo restriction enabled"
  #checkov:skip=CKV_AWS_68:WAFv2 costs about the whole monthly budget of this account.
  #checkov:skip=CKV2_AWS_47:No WAFv2 WebACL is attached.
  #checkov:skip=CKV_AWS_310:One S3 origin and one API origin are enough.
  #checkov:skip=CKV_AWS_86:Legacy CloudFront access logs need a bucket ACL. API Gateway and Lambda log to CloudWatch instead.
  #checkov:skip=CKV_AWS_174:The default CloudFront certificate does not allow choosing the TLS policy.
  #checkov:skip=CKV2_AWS_42:The demo runs on the *.cloudfront.net address with its default certificate, without a domain of its own.
  #checkov:skip=CKV_AWS_305:Geo restriction is not required for this private app.
  provider            = aws.global
  enabled             = true
  is_ipv6_enabled     = true
  comment             = local.name
  price_class         = "PriceClass_100"
  default_root_object = "index.html"
  http_version        = "http2and3"
  wait_for_deployment = true

  origin {
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_id                = "web"
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
    connection_attempts      = 3
    connection_timeout       = 10
  }

  origin {
    domain_name              = aws_s3_bucket.media.bucket_regional_domain_name
    origin_id                = "media"
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
    connection_attempts      = 3
    connection_timeout       = 10
  }

  origin {
    domain_name = "${aws_apigatewayv2_api.http.id}.execute-api.${var.region}.amazonaws.com"
    origin_id   = "api"

    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "https-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id       = "web"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    cache_policy_id            = aws_cloudfront_cache_policy.web.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.web.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.routes.arn
    }
  }

  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "api"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    viewer_protocol_policy   = "redirect-to-https"
    compress                 = true
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.api_host.arn
    }
  }

  ordered_cache_behavior {
    path_pattern           = "/media/*"
    target_origin_id       = "media"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https"
    compress               = true
    cache_policy_id        = aws_cloudfront_cache_policy.media.id
    trusted_key_groups     = [aws_cloudfront_key_group.media.id]

    response_headers_policy_id = aws_cloudfront_response_headers_policy.media.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.media_path.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # The demo lives on the CloudFront address, so the default certificate is
  # used. A custom domain would add an ACM certificate and an alias here.
  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
