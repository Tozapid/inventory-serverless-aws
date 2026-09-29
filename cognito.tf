resource "aws_cognito_user_pool" "main" {
  name                     = local.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  # Each user turns on an authenticator app in the settings if they want to.
  mfa_configuration = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  password_policy {
    minimum_length                   = var.password_min_length
    require_lowercase                = true
    require_uppercase                = false
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  admin_create_user_config {
    allow_admin_create_user_only = true

    invite_message_template {
      email_subject = "Box inventory invitation"
      email_message = "You have been invited to the box inventory at https://${aws_cloudfront_distribution.web.domain_name}<br><br>Sign-in: {username}<br>Temporary password: {####}<br><br>You will choose your own password at first sign-in. The temporary one works for 7 days."
      sms_message   = "Box inventory: sign-in {username}, temporary password {####}"
    }
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }
}

resource "aws_cognito_user_pool_client" "web" {
  name         = "${local.name}-web"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret = false
  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_PASSWORD_AUTH",
  ]

  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }

  read_attributes = ["email", "email_verified"]
  # The email names the user in the API, so users cannot change it. An empty
  # list would let the client write every standard attribute, hence locale.
  write_attributes = ["locale"]
}

resource "aws_cognito_user" "admin" {
  user_pool_id   = aws_cognito_user_pool.main.id
  username       = var.admin_email
  password       = var.admin_password
  message_action = "SUPPRESS"

  attributes = {
    email          = var.admin_email
    email_verified = "true"
  }
}

# Members of this group see user management in the settings.
resource "aws_cognito_user_group" "admins" {
  name         = "admins"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Administrators: invite, disable and delete users, reset two-step sign-in."
}

resource "aws_cognito_user_in_group" "admin" {
  user_pool_id = aws_cognito_user_pool.main.id
  group_name   = aws_cognito_user_group.admins.name
  username     = aws_cognito_user.admin.username
}
