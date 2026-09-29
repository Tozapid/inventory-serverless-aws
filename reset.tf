# Public demo: every hour the inventory goes back to a fresh deployment.
# Set reset_schedule to "" to keep data.

resource "aws_ssm_parameter" "admin_password" {
  #checkov:skip=CKV_AWS_337:The AWS managed SSM key is enough for a demo password that the README publishes.
  name        = "/${local.name}/admin-password"
  description = "Password the hourly reset gives back to the demo administrator."
  type        = "SecureString"
  value       = var.admin_password
}

# The reset writes the example boxes with the API's own item builders.
data "archive_file" "reset" {
  type        = "zip"
  output_path = "${path.module}/.build/reset.zip"

  source {
    content  = file("${path.module}/lambda/reset.py")
    filename = "reset.py"
  }

  source {
    content  = file("${path.module}/lambda/lambda_app.py")
    filename = "lambda_app.py"
  }

  source {
    content  = file("${path.module}/seed/boxes.json")
    filename = "boxes.json"
  }
}

resource "aws_cloudwatch_log_group" "reset" {
  #checkov:skip=CKV_AWS_158:CloudWatch encryption with a customer key is not worth a monthly charge here.
  #checkov:skip=CKV_AWS_338:Thirty days is enough for reset logs.
  name              = "/aws/lambda/${local.name}-reset"
  retention_in_days = 30
}

data "aws_iam_policy_document" "reset" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.reset.arn}:*"]
  }

  statement {
    sid       = "Table"
    actions   = ["dynamodb:Scan", "dynamodb:BatchWriteItem"]
    resources = [aws_dynamodb_table.boxes.arn]
  }

  statement {
    sid       = "Photos"
    actions   = ["s3:ListBucketVersions", "s3:ListBucket"]
    resources = [aws_s3_bucket.media.arn]
  }

  statement {
    sid       = "PhotoVersions"
    actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
    resources = ["${aws_s3_bucket.media.arn}/*"]
  }

  statement {
    sid = "Users"
    actions = [
      "cognito-idp:ListUsers",
      "cognito-idp:AdminDeleteUser",
      "cognito-idp:AdminCreateUser",
      "cognito-idp:AdminSetUserPassword",
      "cognito-idp:AdminSetUserMFAPreference",
      "cognito-idp:AdminEnableUser",
      "cognito-idp:AdminAddUserToGroup",
      "cognito-idp:AdminUserGlobalSignOut",
    ]
    resources = [aws_cognito_user_pool.main.arn]
  }

  statement {
    sid       = "AdminPassword"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.admin_password.arn]
  }
}

resource "aws_iam_role" "reset" {
  name               = "${local.name}-reset"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "reset" {
  name   = "${local.name}-reset"
  role   = aws_iam_role.reset.id
  policy = data.aws_iam_policy_document.reset.json
}

resource "aws_lambda_function" "reset" {
  #checkov:skip=CKV_AWS_50:X-Ray is off to keep the demo free.
  #checkov:skip=CKV_AWS_115:Reserved concurrency is left to the account pool.
  #checkov:skip=CKV_AWS_116:A failed reset simply runs again next hour.
  #checkov:skip=CKV_AWS_117:The function only calls public AWS endpoints.
  #checkov:skip=CKV_AWS_173:Environment values are resource names, not secrets.
  #checkov:skip=CKV_AWS_272:Code signing is not used here.
  function_name = "${local.name}-reset"
  role          = aws_iam_role.reset.arn
  runtime       = "python3.14"
  architectures = ["arm64"]
  handler       = "reset.handler"
  memory_size   = 256
  timeout       = 300

  filename         = data.archive_file.reset.output_path
  source_code_hash = data.archive_file.reset.output_base64sha256

  environment {
    variables = {
      TABLE_NAME               = aws_dynamodb_table.boxes.name
      MEDIA_BUCKET             = aws_s3_bucket.media.id
      USER_POOL_ID             = aws_cognito_user_pool.main.id
      ADMIN_EMAIL              = var.admin_email
      ADMIN_PASSWORD_PARAMETER = aws_ssm_parameter.admin_password.name
      ADMIN_GROUP              = aws_cognito_user_group.admins.name
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.reset.name
  }

  depends_on = [aws_iam_role_policy.reset]
}

resource "aws_cloudwatch_event_rule" "reset" {
  count               = var.reset_schedule == "" ? 0 : 1
  name                = "${local.name}-reset"
  description         = "Wipe the demo inventory back to its initial state."
  schedule_expression = var.reset_schedule
}

resource "aws_cloudwatch_event_target" "reset" {
  count = var.reset_schedule == "" ? 0 : 1
  rule  = aws_cloudwatch_event_rule.reset[0].name
  arn   = aws_lambda_function.reset.arn
}

resource "aws_lambda_permission" "reset" {
  count         = var.reset_schedule == "" ? 0 : 1
  statement_id  = "AllowHourlyReset"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reset.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reset[0].arn
}
