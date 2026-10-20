# Access logs of the distribution (standard logging v2), when
# access_logs_bucket is set: a JSON line per request, gzipped, under
# cloudfront/<name>/<yyyy>/<MM>/<dd>/ of that bucket, which an overview of
# the account (such as Tozapid/account-serverless-aws) can read. CloudWatch
# delivers them from us-east-1, also to a bucket of another region, at no
# charge; the bucket policy has to let delivery.logs.amazonaws.com write.
resource "aws_cloudwatch_log_delivery_source" "access" {
  count = var.access_logs_bucket != "" ? 1 : 0

  provider     = aws.global
  name         = "${local.name}-cloudfront"
  log_type     = "ACCESS_LOGS"
  resource_arn = aws_cloudfront_distribution.web.arn
}

resource "aws_cloudwatch_log_delivery_destination" "access" {
  count = var.access_logs_bucket != "" ? 1 : 0

  provider      = aws.global
  name          = "${local.name}-cloudfront"
  output_format = "json"

  delivery_destination_configuration {
    destination_resource_arn = "arn:aws:s3:::${var.access_logs_bucket}/cloudfront/${local.name}"
  }
}

resource "aws_cloudwatch_log_delivery" "access" {
  count = var.access_logs_bucket != "" ? 1 : 0

  provider                 = aws.global
  delivery_source_name     = aws_cloudwatch_log_delivery_source.access[0].name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.access[0].arn

  # When, who and from where, what, the answer and how the cache served it.
  record_fields = [
    "date", "time", "c-ip", "c-country", "cs-method", "cs-uri-stem", "sc-status", "sc-bytes",
    "time-taken", "cs(Referer)", "cs(User-Agent)", "x-edge-result-type", "x-host-header",
  ]

  s3_delivery_configuration {
    suffix_path                 = "{yyyy}/{MM}/{dd}"
    enable_hive_compatible_path = false
  }
}
