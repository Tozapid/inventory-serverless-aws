# The demo kept its resources at the top of the state until the repository
# became a module (v1.8.0): its state keeps every resource, now under
# module.inventory. The resources of the reset are made only with a
# reset_schedule, so they have an index there.

moved {
  from = aws_cloudwatch_log_delivery_source.access
  to   = module.inventory.aws_cloudwatch_log_delivery_source.access
}

moved {
  from = aws_cloudwatch_log_delivery_destination.access
  to   = module.inventory.aws_cloudwatch_log_delivery_destination.access
}

moved {
  from = aws_cloudwatch_log_delivery.access
  to   = module.inventory.aws_cloudwatch_log_delivery.access
}

moved {
  from = aws_apigatewayv2_api.http
  to   = module.inventory.aws_apigatewayv2_api.http
}

moved {
  from = aws_apigatewayv2_authorizer.cognito
  to   = module.inventory.aws_apigatewayv2_authorizer.cognito
}

moved {
  from = aws_apigatewayv2_integration.lambda
  to   = module.inventory.aws_apigatewayv2_integration.lambda
}

moved {
  from = aws_apigatewayv2_route.api
  to   = module.inventory.aws_apigatewayv2_route.api
}

moved {
  from = aws_cloudwatch_log_group.api
  to   = module.inventory.aws_cloudwatch_log_group.api
}

moved {
  from = aws_apigatewayv2_stage.default
  to   = module.inventory.aws_apigatewayv2_stage.default
}

moved {
  from = aws_lambda_permission.api
  to   = module.inventory.aws_lambda_permission.api
}

moved {
  from = tls_private_key.media
  to   = module.inventory.tls_private_key.media
}

moved {
  from = aws_cloudfront_public_key.media
  to   = module.inventory.aws_cloudfront_public_key.media
}

moved {
  from = aws_cloudfront_key_group.media
  to   = module.inventory.aws_cloudfront_key_group.media
}

moved {
  from = aws_ssm_parameter.media_signing_key
  to   = module.inventory.aws_ssm_parameter.media_signing_key
}

moved {
  from = aws_ssm_parameter.media_key
  to   = module.inventory.aws_ssm_parameter.media_key
}

moved {
  from = aws_cloudfront_function.api_host
  to   = module.inventory.aws_cloudfront_function.api_host
}

moved {
  from = aws_cloudfront_function.media_path
  to   = module.inventory.aws_cloudfront_function.media_path
}

moved {
  from = aws_cloudfront_cache_policy.media
  to   = module.inventory.aws_cloudfront_cache_policy.media
}

moved {
  from = aws_cloudfront_response_headers_policy.media
  to   = module.inventory.aws_cloudfront_response_headers_policy.media
}

moved {
  from = aws_cloudfront_origin_access_control.web
  to   = module.inventory.aws_cloudfront_origin_access_control.web
}

moved {
  from = aws_cloudfront_cache_policy.web
  to   = module.inventory.aws_cloudfront_cache_policy.web
}

moved {
  from = aws_cloudfront_response_headers_policy.web
  to   = module.inventory.aws_cloudfront_response_headers_policy.web
}

moved {
  from = aws_cloudfront_function.routes
  to   = module.inventory.aws_cloudfront_function.routes
}

moved {
  from = aws_cloudfront_distribution.web
  to   = module.inventory.aws_cloudfront_distribution.web
}

moved {
  from = aws_cognito_user_pool.main
  to   = module.inventory.aws_cognito_user_pool.main
}

moved {
  from = aws_cognito_user_pool_client.web
  to   = module.inventory.aws_cognito_user_pool_client.web
}

moved {
  from = aws_cognito_user.admin
  to   = module.inventory.aws_cognito_user.admin
}

moved {
  from = aws_cognito_user_group.admins
  to   = module.inventory.aws_cognito_user_group.admins
}

moved {
  from = aws_cognito_user_in_group.admin
  to   = module.inventory.aws_cognito_user_in_group.admin
}

moved {
  from = aws_dynamodb_table.boxes
  to   = module.inventory.aws_dynamodb_table.boxes
}

moved {
  from = aws_s3_object.web
  to   = module.inventory.aws_s3_object.web
}

moved {
  from = aws_s3_object.config
  to   = module.inventory.aws_s3_object.config
}

moved {
  from = aws_iam_role.lambda
  to   = module.inventory.aws_iam_role.lambda
}

moved {
  from = aws_iam_role_policy.lambda
  to   = module.inventory.aws_iam_role_policy.lambda
}

moved {
  from = aws_cloudwatch_log_group.lambda
  to   = module.inventory.aws_cloudwatch_log_group.lambda
}

moved {
  from = aws_lambda_function.api
  to   = module.inventory.aws_lambda_function.api
}

moved {
  from = aws_ssm_parameter.admin_password
  to   = module.inventory.aws_ssm_parameter.admin_password[0]
}

moved {
  from = aws_cloudwatch_log_group.reset
  to   = module.inventory.aws_cloudwatch_log_group.reset[0]
}

moved {
  from = aws_iam_role.reset
  to   = module.inventory.aws_iam_role.reset[0]
}

moved {
  from = aws_iam_role_policy.reset
  to   = module.inventory.aws_iam_role_policy.reset[0]
}

moved {
  from = aws_lambda_function.reset
  to   = module.inventory.aws_lambda_function.reset[0]
}

moved {
  from = aws_cloudwatch_event_rule.reset
  to   = module.inventory.aws_cloudwatch_event_rule.reset
}

moved {
  from = aws_cloudwatch_event_target.reset
  to   = module.inventory.aws_cloudwatch_event_target.reset
}

moved {
  from = aws_lambda_permission.reset
  to   = module.inventory.aws_lambda_permission.reset
}

moved {
  from = aws_s3_bucket.web
  to   = module.inventory.aws_s3_bucket.web
}

moved {
  from = aws_s3_bucket_public_access_block.web
  to   = module.inventory.aws_s3_bucket_public_access_block.web
}

moved {
  from = aws_s3_bucket_ownership_controls.web
  to   = module.inventory.aws_s3_bucket_ownership_controls.web
}

moved {
  from = aws_s3_bucket_versioning.web
  to   = module.inventory.aws_s3_bucket_versioning.web
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.web
  to   = module.inventory.aws_s3_bucket_server_side_encryption_configuration.web
}

moved {
  from = aws_s3_bucket_lifecycle_configuration.web
  to   = module.inventory.aws_s3_bucket_lifecycle_configuration.web
}

moved {
  from = aws_s3_bucket_policy.web
  to   = module.inventory.aws_s3_bucket_policy.web
}

moved {
  from = aws_s3_bucket.media
  to   = module.inventory.aws_s3_bucket.media
}

moved {
  from = aws_s3_bucket_public_access_block.media
  to   = module.inventory.aws_s3_bucket_public_access_block.media
}

moved {
  from = aws_s3_bucket_ownership_controls.media
  to   = module.inventory.aws_s3_bucket_ownership_controls.media
}

moved {
  from = aws_s3_bucket_versioning.media
  to   = module.inventory.aws_s3_bucket_versioning.media
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.media
  to   = module.inventory.aws_s3_bucket_server_side_encryption_configuration.media
}

moved {
  from = aws_s3_bucket_cors_configuration.media
  to   = module.inventory.aws_s3_bucket_cors_configuration.media
}

moved {
  from = aws_s3_bucket_policy.media
  to   = module.inventory.aws_s3_bucket_policy.media
}

moved {
  from = aws_s3_bucket_lifecycle_configuration.media
  to   = module.inventory.aws_s3_bucket_lifecycle_configuration.media
}

moved {
  from = aws_s3_object.seed
  to   = module.inventory.aws_s3_object.seed
}
