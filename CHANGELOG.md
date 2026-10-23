# Changelog

## v1.6.0

### Added

- Trash: a deleted box is moved to Settings → Trash with its history and photos, after a confirmation that says so. Everyone restores a box from there; administrators delete it for good, history and photos included. The number of a box in the trash stays taken until then. The API keeps the card under `SK=TRASH`, listed from the `boxes` index under `gsi1pk = TRASH`: `GET /api/trash`, `POST /api/trash/<number>/restore`, `DELETE /api/trash/<number>` (administrators).
- Photos in the history: every new or turned photo is an entry with the earlier and the new photo side by side, opened full size on click. Earlier photos stay in the bucket while an entry shows them; deleting the entry deletes such a photo.
- Recognition: a prompt of your own in Settings → Recognition (empty is the built-in one; the answer language is added to it), and "Recognize all photos again" with the language chosen there. The prompt and model are saved first; the API goes through the boxes one at a time in the background (`POST /api/recognition/rerun`, stopped with `DELETE`), the tab shows how far it got, and each new description goes into the history as "Recognized again". The API function's time limit is now 900 s for these runs.
- Leaving a card being edited asks first if anything changed: Cancel, Close, another box in the list, back and forward, the logo, closing the tab. So does hiding the new box form with something typed in it.

### Changed

- With a card open, the list narrows to a column of about a fifth of the page and the card takes the rest, with the photo and the text side by side on a wide card. Without a card the list keeps the page.
- The description and contents fields grow with their text while it is typed.
- The history of a card is folded under its heading, with the number of entries, and opens on a click.

### Fixed

- A photo saved while editing a card wiped the text typed so far about ten seconds later, when recognition finished and the card was drawn again. The photo description and the history are now redrawn on their own; so is the photo description shown or hidden with its button, which wiped the text the same way.
- On an iPhone the page zoomed in on a field and no longer fitted the screen: fields use 16 px text on phones. The header of the page and of the card take less room there, and long words in a card wrap.

## v1.5.0

### Added

- CloudFront access logs, optional: with `access_logs_bucket` set, the standard logs (v2) of the distribution go to that bucket under `cloudfront/<name>/` as JSON lines, for an overview of the account such as [account-serverless-aws](https://github.com/Tozapid/account-serverless-aws) to read. The CI passes the repository variable `ACCESS_LOGS_BUCKET`; unset, there are none.

## v1.4.1

### Fixed

- Settings → Version showed the word "null" under the version when no newer release was out.

### Changed

- A `CHANGELOG.md` entry no longer needs a date: `## v1.4.1`, `## v1.4.1 — 2026-10-12` and `## v1.4.1 — a title` all work. GitHub dates the release itself.

## v1.4.0 — 2026-10-12

### Added

- Settings → Version: the version of the site and the releases of the demo repository that came after it, with their notes. The API reads them from the Atom feed of the GitHub releases (`GET /api/version`) and keeps them an hour.
- Versions: every entry of this changelog starts with `vX.Y.Z`. After `main` is deployed, CI makes the tag and the GitHub release of the newest entry if it is not out yet. The site takes its version from the newest entry when it is deployed; `releases_repository` names the repository to compare with.

## v1.3.0 — 2026-10-12

### Changed

- The sign-in page no longer says it is for invited users only; it links to the source code on GitHub beside the language menu, as Hide My Email does.
- Settings as in Hide My Email: the new password is typed twice; two-factor authentication (the name in place of "two-step sign-in" everywhere) shows its state and one button, and pairs in two numbered steps (scan the code or type the key, then the code from the app); the language is a drop-down, as on the sign-in page.
- `.terraform.lock.hcl` is no longer kept in the repository (it is in `.gitignore`): `terraform init` takes the newest AWS provider within the constraint in `terraform.tf`.

## v1.2.0 — 2026-10-11

### Changed

- CI: the jobs moved to the shared reusable workflow of [Tozapid/demo-ci-cd-skeleton](https://github.com/Tozapid/demo-ci-cd-skeleton); `.github/workflows/terraform.yml` is a short caller, the same in all three projects. The Lambda tests run by discovery (`*_test.py`), the page tests run where `tests/web` exists, the state key is `<repository>/terraform.tfstate`.
- `main` is protected: changes go through pull requests, merged by squash once the checks `ci / Terraform format and validate`, `ci / Lambda tests` and `ci / Frontend tests` pass; merged branches are deleted.
- AWS access through GitHub OIDC: the plan and apply jobs run in the environment `demo` and assume the role in the variable `AWS_ROLE_ARN` (role `github-inventory-serverless-aws`, an entry of `github_roles` in the stack `iam-github-oidc` of the account repository). The secrets `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` and the deploy user with its access key are gone.
- Deploy from a branch: a manual run of the workflow applies the branch it was started from, not only `main`, over the same demo site; the run summary names the branch and warns that the next push to `main` replaces it. Applies from different branches wait for each other.
- The page: one design shared with the sibling projects (Hide My Email, the box inventory and the wiki): the same block of variables and components opens `web/styles.css` in all three and is kept identical — an indigo accent, system fonts, rounded cards, fields, buttons, dialogs, settings, tables and toasts. Light and dark themes follow the system setting. Square corners, the paper colours and serif headings are gone; the box number badge and the selected row use the accent. The photo viewer stays dark in both themes, labels print black on white as before.
- New favicon in the shared style (a box on indigo); it is also the logo on the sign-in card and in the top bar, in place of the letter mark.

## v1.1.0 — 2026-10-10

### Added

- Keyboard shortcuts: ⌘Enter (Ctrl+Enter) saves the card being edited or creates the new box; I, J and L sit like arrow keys: I edits the open card, J opens the previous box of the list as shown and L the next one, loading the next page at its end. Esc leaves editing even from a field, and closes the new box form; if anything was typed, it asks first. Letters go by their place on the keyboard, so they work in any layout, and do nothing while typing, while a card is being edited, or with the settings or the photo viewer open. The buttons show the shortcut on hover.
- Labels: the settings have a Labels tab with every box ticked, "Select all", "Clear all" and a Print button, and the label options: width and height in mm (30 × 50 by default; the A4 grid follows, with 5 mm free at the edges and the grid centred, e.g. 6 × 5 = 30), the QR code size and the font size in mm (empty is automatic: the QR code takes 80% of the label width, at most 55% of its height under text, with as much white above as beside it to cut along; the text takes the largest size that fits, a chosen size is used as it is), the font (monospace, sans serif or serif) and what goes under the QR code (the number, the name — the first line of the description — or both). The options are kept in the browser (`localStorage`, key `inventory-labels`), and a sample label shows them. The QR code links to the card (`https://<site>/box/<number>`); on a wide label it sits on the left with the text beside it. The text takes the largest size that fits, measured in the page; words are not broken, and only a number that would come out under 4 mm may break between letters. Sheets are built in the page, shown only to the printer, with a dashed line to cut along; print at 100% with no margins.
- Sorting of the list: by index (the default), by name, recently changed first or newest first. The index is the box number from its address, `/box/<index>`, with numbers inside compared as numbers: `2` before `12`, `A-9` before `A-10`, `12` before `12a`. The name is the first line of the description without Markdown, as in the card title; case does not matter, `ё` counts as `е`, and boxes without a description go last, by index. The choice is kept in the browser (`localStorage`, key `inventory-sort`) through searches, edits and reloads; sorted by a date, each row shows that date. The API takes `sort=number|name|updated|created` on `GET /api/boxes`, `number` (by index) when left out. It reads every matching card, sorts it and pages by position (the `cursor` is an offset); the `gsi1sk` key of the DynamoDB index is unchanged, so nothing has to be migrated. New boxes get `created_at`; older ones get the time of their first history entry (or of their last change) the first time the list is sorted by it, and keep it. The demo reset writes `created_at` for the example boxes.
- Search in the history, a separate option off by default: the "in history" box next to the search field also looks through earlier contents, comments and photo descriptions. A box found only there is marked "found in history". The API takes it as `history=1` on `GET /api/boxes`.
- The photo description written by recognition goes into the history of the box, signed by whoever uploaded the photo; replacing a photo records its old description going away. History entries carry `field` (`contents` or `hidden_text`), and the card labels each entry.
- Photo rotation: a portrait photo is turned 90° clockwise on upload and stored landscape (`PORTRAIT_TURN` in `web/app.js`); the browser turns it while re-encoding, so the stored files are already turned. While editing a card, ↺ and ↻ turn the saved photo by 90°. A turned photo is saved with `keep_description: true`: its recognized description stays, with no new recognition and no history entry.
- Administrators delete single history entries with the "Delete" button in the entry header, after a confirmation. `GET /api/boxes/<number>/history` returns an `id` for each entry; `DELETE /api/boxes/<number>/history/<id>` removes it, 403 for everyone else.
- Recognition log: every attempt is written per user (`USER#<email>`, `SK=RECOG#...`) with the box, model, outcome, duration and description length. A Grok error keeps its HTTP code and body (up to 2000 characters, with the key cut out); an empty answer keeps `status`, `incomplete_details` and the `output` block types. A replaced photo, a deleted box, a missing key, a timeout and a failed start of the background task are recorded too. An attempt is written as running first; if it has not finished after three minutes, the log shows "Did not finish". The Recognition tab lists the last 50 attempts (`GET /api/settings/recognition-log`); administrators can switch to everyone's last 100 (`GET /api/admin/recognition-log`), read from the `boxes` index under `gsi1pk = RECOG`. DynamoDB TTL on `expires_at` removes entries after 30 days; in the demo the hourly reset wipes them anyway.

### Changed

- New example photos of the demo: drives (box 1), a toolbox (2), a label printer (3) and cables (12), re-encoded without metadata at 2560 and 1280 px. The example boxes and their hidden photo text follow the photos; searching for `spanners` or `rolls` finds a box by its photo. The CC0 photos and their credits are gone.
- Fixed: opening boxes quickly one after another (2 → 3 → 2) let the first, late answer redraw the card, over an edit already started. Each opening now has a ticket and only the latest one draws the card; E waits until the selected card is shown.
- The interface tests serve the page with the Content-Security-Policy from `locals.tf`, as CloudFront does. The page sets inline sizes through the DOM (`style.cssText`), which `style-src 'self'` allows; a `style` attribute is dropped by it, which made the label sample and sheets ignore their sizes on the site.
- The photo view lays tiles out row by row in the order of the list (a CSS grid instead of CSS columns, which filled column by column): up to three columns of at least 220px, two on a phone.
- A row of the list shows the number and the name (the first line of the description) on one line, and the rest of a longer description below it, smaller and without a frame. The number plate no longer stretches across the row.
- "Contents history" on the card is now "History", since it holds photo descriptions too.
- The Grok key is write-only: `GET /api/settings` returns `grok_key_set` and the last four characters in `grok_key_hint` (for keys of 12 characters or more), never the key. The key field in the settings stays empty; saving it empty keeps the stored key, `remove_key: true` deletes it, and the settings have a "Delete key" button.
- GitHub Actions on Node.js 24: `actions/checkout@v7`, `actions/setup-python@v7`, `actions/setup-node@v7`, `hashicorp/setup-terraform@v4`, `aws-actions/configure-aws-credentials@v6`.
- `dynamodb.tf`: the keys of the `boxes` index are `key_schema` blocks instead of the deprecated `hash_key` and `range_key` (AWS provider 6.x).

## v1.0.0 — 2026-09-30

### Added

- First public version, from the private box inventory: boxes with a number, description, contents in Markdown and a photo; search including hidden photo text; history of the contents; optional Grok photo recognition; optional two-step sign-in (TOTP); user administration; interface in English, Russian, French and Italian.
- Demo mode, as in [wiki-serverless-aws](https://github.com/Tozapid/wiki-serverless-aws): the sign-in screen shows and fills in `admin@example.com` / `admin123`; invitations show the temporary password instead of sending email; an hourly reset wipes boxes, photos, history and users, restores the admin (with two-step sign-in off) and writes seven example boxes, four with CC0 photos.
- The `recognition` switch, off in the demo: every visitor shares the admin account and would see a stored API key. The example boxes carry hidden text instead, so search by photo contents still works.
- The site runs on the CloudFront address with its default certificate; no domain of its own is needed.
- Lambda tests and frontend tests (translations; the interface in headless Chrome), run in GitHub Actions before every apply.
