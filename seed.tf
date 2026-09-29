# Example photos of the demo. The hourly reset writes the boxes from
# seed/boxes.json again and leaves seed/ in the media bucket alone. An
# inventory without a reset gets neither.

locals {
  seed_photos = var.reset_schedule == "" ? toset([]) : fileset("${path.module}/seed/photos", "*.jpg")
}

resource "aws_s3_object" "seed" {
  for_each = local.seed_photos

  bucket       = aws_s3_bucket.media.id
  key          = "seed/photos/${each.value}"
  source       = "${path.module}/seed/photos/${each.value}"
  source_hash  = filemd5("${path.module}/seed/photos/${each.value}")
  content_type = "image/jpeg"
}
