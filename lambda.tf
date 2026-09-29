resource "aws_cloudwatch_log_group" "lambda" {
  #checkov:skip=CKV_AWS_158: "Ensure that CloudWatch Log Group is encrypted by KMS"
  #checkov:skip=CKV_AWS_338:Thirty days is enough for this private app and keeps the log bill small.
  name              = "/aws/lambda/${local.name}-api"
  retention_in_days = 30
}

data "archive_file" "api" {
  type        = "zip"
  source_file = "${path.module}/lambda/lambda_app.py"
  output_path = "${path.module}/.build/lambda_app.zip"
  # The same zip on a workstation and in CI: a checkout can leave other file
  # modes, and the mode is part of the hash.
  output_file_mode = "0644"
}

resource "aws_lambda_function" "api" {
  #checkov:skip=CKV_AWS_50:X-Ray is disabled to avoid extra cost on a low-traffic app.
  #checkov:skip=CKV_AWS_115:Reserved concurrency is left to the account pool.
  #checkov:skip=CKV_AWS_116:This function is synchronous. Failures are returned to the caller.
  #checkov:skip=CKV_AWS_117:The function only calls public AWS endpoints for DynamoDB and S3.
  #checkov:skip=CKV_AWS_173:Environment values are resource names, not secrets.
  #checkov:skip=CKV_AWS_272:Code signing is not used for this internal function.
  function_name = "${local.name}-api"
  role          = aws_iam_role.lambda.arn
  runtime       = "python3.14"
  architectures = ["arm64"]
  handler       = "lambda_app.handler"
  memory_size   = 256
  timeout       = 90

  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  environment {
    variables = {
      TABLE_NAME               = aws_dynamodb_table.boxes.name
      MEDIA_BUCKET             = aws_s3_bucket.media.id
      URL_TTL_SECONDS          = tostring(var.url_ttl_seconds)
      PHOTO_MAX_BYTES          = tostring(var.photo_max_bytes)
      CLOUDFRONT_KEY_PAIR_ID   = aws_cloudfront_public_key.media.id
      MEDIA_KEY_PARAMETER_NAME = aws_ssm_parameter.media_signing_key.name
      USER_POOL_ID             = aws_cognito_user_pool.main.id
      ADMIN_GROUP              = aws_cognito_user_group.admins.name
      INVITE_EMAILS            = var.invite_emails ? "true" : "false"
      RECOGNITION              = var.recognition ? "true" : "false"
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda.name
  }

  depends_on = [aws_iam_role_policy.lambda]
}
