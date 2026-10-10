# Changelog

## 2026-10-10

### Added

- Sorting of the list: by index (the default), by name, recently changed first or newest first. The index is the box number from its address, `/box/<index>`, with numbers inside compared as numbers: `2` before `12`, `A-9` before `A-10`, `12` before `12a`. The name is the first line of the description without Markdown, as in the card title; case does not matter, `ё` counts as `е`, and boxes without a description go last, by index. The choice is kept in the browser (`localStorage`, key `inventory-sort`) through searches, edits and reloads; sorted by a date, each row shows that date. The API takes `sort=number|name|updated|created` on `GET /api/boxes`, `number` (by index) when left out. It reads every matching card, sorts it and pages by position (the `cursor` is an offset); the `gsi1sk` key of the DynamoDB index is unchanged, so nothing has to be migrated. New boxes get `created_at`; older ones get the time of their first history entry (or of their last change) the first time the list is sorted by it, and keep it. The demo reset writes `created_at` for the example boxes.
- Search in the history, a separate option off by default: the "in history" box next to the search field also looks through earlier contents, comments and photo descriptions. A box found only there is marked "found in history". The API takes it as `history=1` on `GET /api/boxes`.
- The photo description written by recognition goes into the history of the box, signed by whoever uploaded the photo; replacing a photo records its old description going away. History entries carry `field` (`contents` or `hidden_text`), and the card labels each entry.
- Photo rotation: a portrait photo is turned 90° clockwise on upload and stored landscape (`PORTRAIT_TURN` in `web/app.js`); the browser turns it while re-encoding, so the stored files are already turned. While editing a card, ↺ and ↻ turn the saved photo by 90°. A turned photo is saved with `keep_description: true`: its recognized description stays, with no new recognition and no history entry.
- Administrators delete single history entries with the "Delete" button in the entry header, after a confirmation. `GET /api/boxes/<number>/history` returns an `id` for each entry; `DELETE /api/boxes/<number>/history/<id>` removes it, 403 for everyone else.
- Recognition log: every attempt is written per user (`USER#<email>`, `SK=RECOG#...`) with the box, model, outcome, duration and description length. A Grok error keeps its HTTP code and body (up to 2000 characters, with the key cut out); an empty answer keeps `status`, `incomplete_details` and the `output` block types. A replaced photo, a deleted box, a missing key, a timeout and a failed start of the background task are recorded too. An attempt is written as running first; if it has not finished after three minutes, the log shows "Did not finish". The Recognition tab lists the last 50 attempts (`GET /api/settings/recognition-log`); administrators can switch to everyone's last 100 (`GET /api/admin/recognition-log`), read from the `boxes` index under `gsi1pk = RECOG`. DynamoDB TTL on `expires_at` removes entries after 30 days; in the demo the hourly reset wipes them anyway.

### Changed

- "Contents history" on the card is now "History", since it holds photo descriptions too.
- The Grok key is write-only: `GET /api/settings` returns `grok_key_set` and the last four characters in `grok_key_hint` (for keys of 12 characters or more), never the key. The key field in the settings stays empty; saving it empty keeps the stored key, `remove_key: true` deletes it, and the settings have a "Delete key" button.
- GitHub Actions on Node.js 24: `actions/checkout@v7`, `actions/setup-python@v7`, `actions/setup-node@v7`, `hashicorp/setup-terraform@v4`, `aws-actions/configure-aws-credentials@v6`.
- `dynamodb.tf`: the keys of the `boxes` index are `key_schema` blocks instead of the deprecated `hash_key` and `range_key` (AWS provider 6.x).

## 2026-09-30

### Added

- First public version, from the private box inventory: boxes with a number, description, contents in Markdown and a photo; search including hidden photo text; history of the contents; optional Grok photo recognition; optional two-step sign-in (TOTP); user administration; interface in English, Russian, French and Italian.
- Demo mode, as in [wiki-serverless-aws](https://github.com/Tozapid/wiki-serverless-aws): the sign-in screen shows and fills in `admin@example.com` / `admin123`; invitations show the temporary password instead of sending email; an hourly reset wipes boxes, photos, history and users, restores the admin (with two-step sign-in off) and writes seven example boxes, four with CC0 photos.
- The `recognition` switch, off in the demo: every visitor shares the admin account and would see a stored API key. The example boxes carry hidden text instead, so search by photo contents still works.
- The site runs on the CloudFront address with its default certificate; no domain of its own is needed.
- Lambda tests and frontend tests (translations; the interface in headless Chrome), run in GitHub Actions before every apply.
