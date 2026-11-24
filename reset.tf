# Public demo: reset_delay_hours after a change the inventory goes back to a
# fresh deployment. Set reset_delay_hours to 0 to keep the data (and none of
# this is made).

resource "aws_ssm_parameter" "admin_password" {
  #checkov:skip=CKV_AWS_337:The AWS managed SSM key is enough for a demo password that the README publishes.
  count = local.reset ? 1 : 0

  name        = "/${local.name}/admin-password"
  description = "Password the reset gives back to the demo administrator."
  type        = "SecureString"
  value       = var.admin_password

  lifecycle {
    precondition {
      condition     = var.admin_password != ""
      error_message = "The reset gives the demo administrator admin_password back: set one, or turn the reset off with reset_delay_hours = 0."
    }
  }
}

# The reset writes the example boxes with the API's own item builders.
data "archive_file" "reset" {
  type        = "zip"
  output_path = "${path.root}/.build/${local.name}-reset.zip"

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
  count = local.reset ? 1 : 0

  name              = "/aws/lambda/${local.name}-reset"
  retention_in_days = 30
}

data "aws_iam_policy_document" "reset" {
  count = local.reset ? 1 : 0

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.reset[0].arn}:*"]
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
    resources = [aws_ssm_parameter.admin_password[0].arn]
  }
}

resource "aws_iam_role" "reset" {
  count = local.reset ? 1 : 0

  name               = "${local.name}-reset"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "reset" {
  count = local.reset ? 1 : 0

  name   = "${local.name}-reset"
  role   = aws_iam_role.reset[0].id
  policy = data.aws_iam_policy_document.reset[0].json
}

resource "aws_lambda_function" "reset" {
  #checkov:skip=CKV_AWS_50:X-Ray is off to keep the demo free.
  #checkov:skip=CKV_AWS_115:Reserved concurrency is left to the account pool.
  #checkov:skip=CKV_AWS_116:A failed reset simply runs again next hour.
  #checkov:skip=CKV_AWS_117:The function only calls public AWS endpoints.
  #checkov:skip=CKV_AWS_173:Environment values are resource names, not secrets.
  #checkov:skip=CKV_AWS_272:Code signing is not used here.
  count = local.reset ? 1 : 0

  function_name = "${local.name}-reset"
  role          = aws_iam_role.reset[0].arn
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
      ADMIN_PASSWORD_PARAMETER = aws_ssm_parameter.admin_password[0].name
      ADMIN_GROUP              = aws_cognito_user_group.admins.name
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.reset[0].name
  }

  depends_on = [aws_iam_role_policy.reset]
}

# The timer of the reset. Nothing runs on a schedule: the first change through
# the API (arm_reset in the function's code) makes a one-off schedule
# <name>-reset of EventBridge Scheduler, which calls the reset function
# reset_delay_hours later with this role and then deletes itself. A demo that
# nobody changes is not reset, and costs nothing.
locals {
  reset_schedule_name      = "${local.name}-reset"
  reset_function_arn       = "arn:aws:lambda:${local.region}:${local.account_id}:function:${local.name}-reset"
  reset_scheduler_role_arn = "arn:aws:iam::${local.account_id}:role/${local.name}-reset-scheduler"
}

data "aws_iam_policy_document" "reset_scheduler_assume" {
  count = local.reset ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "reset_scheduler" {
  count = local.reset ? 1 : 0

  name               = "${local.name}-reset-scheduler"
  assume_role_policy = data.aws_iam_policy_document.reset_scheduler_assume[0].json
}

resource "aws_iam_role_policy" "reset_scheduler" {
  count = local.reset ? 1 : 0

  name = "${local.name}-reset-scheduler"
  role = aws_iam_role.reset_scheduler[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.reset[0].arn
    }]
  })
}
