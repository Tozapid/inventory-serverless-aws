locals {
  web_assets = {
    "index.html"  = "text/html; charset=utf-8"
    "app.js"      = "text/javascript; charset=utf-8"
    "i18n.js"     = "text/javascript; charset=utf-8"
    "styles.css"  = "text/css; charset=utf-8"
    "favicon.svg" = "image/svg+xml"
  }
}

resource "aws_s3_object" "web" {
  for_each = local.web_assets

  bucket        = aws_s3_bucket.web.id
  key           = each.key
  source        = "${path.module}/web/${each.key}"
  etag          = filemd5("${path.module}/web/${each.key}")
  content_type  = each.value
  cache_control = contains(["styles.css", "favicon.svg"], each.key) ? "public, max-age=60" : "no-cache"
}

resource "aws_s3_object" "config" {
  bucket        = aws_s3_bucket.web.id
  key           = "config.js"
  content_type  = "text/javascript; charset=utf-8"
  cache_control = "no-cache"
  content = templatefile("${path.module}/web/config.js.tftpl", {
    region       = var.region
    user_pool_id = aws_cognito_user_pool.main.id
    client_id    = aws_cognito_user_pool_client.web.id
    api_base     = "/api"
    # The admin sign-in reaches the page only in demo mode.
    demo_email  = var.demo ? var.admin_email : ""
    demo_pass   = var.demo ? var.admin_password : ""
    reset_every = var.reset_schedule
    recognition = var.recognition
  })
}
