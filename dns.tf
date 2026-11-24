# A domain of the page lives in this zone; the demo has none and touches no
# DNS.
data "aws_route53_zone" "web" {
  count        = local.custom_domain ? 1 : 0
  name         = var.route53_zone_name
  private_zone = false

  lifecycle {
    precondition {
      condition     = var.route53_zone_name != ""
      error_message = "domain_name needs route53_zone_name, the public Route 53 zone it is in."
    }
  }
}

resource "aws_acm_certificate" "web" {
  count             = local.custom_domain ? 1 : 0
  provider          = aws.global
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "certificate" {
  for_each = {
    for dvo in local.custom_domain ? aws_acm_certificate.web[0].domain_validation_options : [] : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  allow_overwrite = true
  zone_id         = data.aws_route53_zone.web[0].zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
}

resource "aws_acm_certificate_validation" "web" {
  count                   = local.custom_domain ? 1 : 0
  provider                = aws.global
  certificate_arn         = aws_acm_certificate.web[0].arn
  validation_record_fqdns = [for record in aws_route53_record.certificate : record.fqdn]

  timeouts {
    create = "20m"
  }
}

resource "aws_route53_record" "web" {
  for_each = local.custom_domain ? toset(["A", "AAAA"]) : toset([])

  allow_overwrite = true
  zone_id         = data.aws_route53_zone.web[0].zone_id
  name            = var.domain_name
  type            = each.key

  alias {
    name                   = aws_cloudfront_distribution.web.domain_name
    zone_id                = aws_cloudfront_distribution.web.hosted_zone_id
    evaluate_target_health = false
  }
}
