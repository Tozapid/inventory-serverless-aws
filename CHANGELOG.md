# Changelog

## 2026-09-30

### Added

- First public version, from the private box inventory: boxes with a number, description, contents in Markdown and a photo; search including hidden photo text; history of the contents; optional Grok photo recognition; optional two-step sign-in (TOTP); user administration; interface in English, Russian, French and Italian.
- Demo mode, as in [wiki-serverless-aws](https://github.com/Tozapid/wiki-serverless-aws): the sign-in screen shows and fills in `admin@example.com` / `admin123`; invitations show the temporary password instead of sending email; an hourly reset wipes boxes, photos, history and users, restores the admin (with two-step sign-in off) and writes seven example boxes, four with CC0 photos.
- The `recognition` switch, off in the demo: every visitor shares the admin account and would see a stored API key. The example boxes carry hidden text instead, so search by photo contents still works.
- The site runs on the CloudFront address with its default certificate; no domain of its own is needed.
- Lambda tests and frontend tests (translations; the interface in headless Chrome), run in GitHub Actions before every apply.
