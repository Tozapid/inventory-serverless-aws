// The page in Chrome against a fake API. Chrome loads web/ as it is; every
// request is answered from here: the page files, a stand-in for the Cognito
// library, the example photos and an in-memory API that behaves like
// lambda/lambda_app.py with the example boxes from seed/boxes.json.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const puppeteer = require("puppeteer-core");
const { WEB, SEED } = require("./load");

const ORIGIN = "https://inventory.test";
const ADMIN = { email: "admin@example.com", password: "admin123" };
const TOTP_CODE = "123456";
const TOTP_SECRET = "JBSWY3DPEHPK3PXP";

function chromePath() {
  const candidates = [
    process.env.CHROME_PATH,
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ];
  return candidates.find((file) => file && fs.existsSync(file));
}

function config(recognition) {
  return "window.INVENTORY_CONFIG = " + JSON.stringify({
    region: "eu-central-1",
    userPoolId: "eu-central-1_test",
    clientId: "test",
    apiBase: "/api",
    recognition,
    demo: { email: ADMIN.email, password: ADMIN.password, reset: "rate(1 hour)" },
  }) + ";";
}

// Enough of amazon-cognito-identity-js for the page: one user, the session kept in localStorage.
// "test-totp" in localStorage means the user has an authenticator app; its code is always 123456.
const COGNITO = `(() => {
  const KEY = "test-session";
  const TOTP = "test-totp";
  const session = () => ({
    isValid: () => true,
    getRefreshToken: () => "refresh",
    getIdToken: () => ({ getJwtToken: () => "token", payload: { email: ${JSON.stringify(ADMIN.email)}, "cognito:groups": ["admins"] } }),
  });
  const user = () => ({
    getSession: (done) => done(null, session()),
    refreshSession: (token, done) => done(null, session()),
    signOut: () => localStorage.removeItem(KEY),
    changePassword: (oldPassword, newPassword, done) => done(null, "SUCCESS"),
    getUserData: (done) => done(null, { UserMFASettingList: localStorage.getItem(TOTP) ? ["SOFTWARE_TOKEN_MFA"] : [] }),
    associateSoftwareToken: (callbacks) => callbacks.associateSecretCode(${JSON.stringify(TOTP_SECRET)}),
    verifySoftwareToken: (code, name, callbacks) => (code === ${JSON.stringify(TOTP_CODE)}
      ? callbacks.onSuccess({ Status: "SUCCESS" })
      : callbacks.onFailure({ code: "EnableSoftwareTokenMFAException", message: "Code mismatch" })),
    setUserMfaPreference: (sms, totp, done) => {
      if (totp.Enabled) localStorage.setItem(TOTP, "1");
      else localStorage.removeItem(TOTP);
      done(null, "SUCCESS");
    },
  });
  window.AmazonCognitoIdentity = {
    CognitoUserPool: function () { this.getCurrentUser = () => (localStorage.getItem(KEY) ? user() : null); },
    AuthenticationDetails: function (data) { this.data = data; },
    CognitoUser: function () {
      this.authenticateUser = (details, callbacks) => {
        if (details.data.Password !== ${JSON.stringify(ADMIN.password)}) {
          return callbacks.onFailure({ code: "NotAuthorizedException", message: "Incorrect username or password." });
        }
        if (localStorage.getItem(TOTP)) return callbacks.totpRequired("SOFTWARE_TOKEN_MFA", {});
        localStorage.setItem(KEY, "1");
        callbacks.onSuccess(session());
      };
      this.sendMFACode = (code, callbacks, type) => {
        if (type !== "SOFTWARE_TOKEN_MFA" || code !== ${JSON.stringify(TOTP_CODE)}) {
          return callbacks.onFailure({ code: "CodeMismatchException", message: "Invalid code received for user" });
        }
        localStorage.setItem(KEY, "1");
        callbacks.onSuccess(session());
      };
    },
  };
})();`;

const QRCODE = fs.readFileSync(require.resolve("qrcode-generator/qrcode.js"));

// The Content-Security-Policy CloudFront sends with the page, read from
// locals.tf, so that the page is tested under the same rules as the site.
const CSP = [...fs.readFileSync(path.join(__dirname, "..", "..", "locals.tf"), "utf8")
  .match(/csp = join\("; ", \[([\s\S]*?)\]\)/)[1]
  .matchAll(/"([^"]+)"/g)]
  .map((m) => m[1].replace(/\$\{var\.region\}/g, "eu-central-1"))
  .join("; ");
const TYPES = { ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html", ".jpg": "image/jpeg" };
const fold = (text) => String(text || "").toLowerCase().replace(/ё/g, "е").replace(/\s+/g, " ").trim();

// The API, kept in memory and filled like the hourly reset does. `calls` records every request.
function fakeApi() {
  const seed = JSON.parse(fs.readFileSync(path.join(SEED, "boxes.json"), "utf8")).boxes;
  const boxes = new Map();
  const history = new Map();
  const calls = [];
  const users = [
    { email: ADMIN.email, status: "CONFIRMED", enabled: true, admin: true, mfa: false, self: true },
    { email: "guest@example.com", status: "CONFIRMED", enabled: true, admin: false, mfa: true, self: false },
  ];
  const card = (box) => ({
    number: box.number,
    description: box.description,
    contents: box.contents,
    contents_preview: box.contents.slice(0, 160),
    has_photo: !!box.photo,
    thumb_url: box.photo ? `/media/seed/photos/${box.photo}-thumb.jpg` : "",
    photo_url: box.photo ? `/media/seed/photos/${box.photo}.jpg` : "",
    hidden_text: box.hidden || "",
    updated_at: box.updated || "2026-09-01T10:00:00Z",
    created_at: box.created || "2026-09-01T10:00:00Z",
    updated_by: ADMIN.email,
    version: 1,
    urls_expire_at: "2026-09-01T11:00:00Z",
  });
  // Later example boxes count as added later.
  seed.forEach((box, index) => {
    box.created = "2026-0" + (index + 1) + "-01T10:00:00Z";
    boxes.set(box.number, box);
    history.set(box.number, box.history.map((entry, index) => ({ id: "2026-09-01T10:00:00.000Z#0000000" + index, at: "2026-09-01T10:00:00Z", by: ADMIN.email, field: "contents", before: entry.before, after: entry.after, note: entry.note })).reverse());
  });

  function handle(method, url, body) {
    const route = method + " " + url.pathname.replace(/^\/api/, "");
    calls.push({ route, query: Object.fromEntries(url.searchParams), body });
    const one = /^\/boxes\/([^/]+)$/.exec(url.pathname.replace(/^\/api/, ""));
    const hist = /^\/boxes\/([^/]+)\/history$/.exec(url.pathname.replace(/^\/api/, ""));
    const histEntry = /^\/boxes\/([^/]+)\/history\/([^/]+)$/.exec(url.pathname.replace(/^\/api/, ""));
    if (route === "GET /boxes") {
      const words = fold(url.searchParams.get("q")).split(" ").filter(Boolean);
      const matches = (parts) => words.every((word) => fold(parts.join(" ")).includes(word));
      const items = [];
      for (const box of boxes.values()) {
        if (matches([box.number, box.description, box.contents, box.hidden])) items.push(card(box));
        else if (url.searchParams.get("history") === "1" && (history.get(box.number) || []).some((entry) => matches([box.number, entry.note, entry.before, entry.after]))) {
          items.push({ ...card(box), history_match: true });
        }
      }
      // By number unless asked otherwise, as the API does; the example boxes are
      // already in that order. The name is the first line of the description.
      const sort = url.searchParams.get("sort");
      const name = (item) => fold((String(item.description).split("\n").find((line) => line.trim()) || "").replace(/[#*_`]/g, ""));
      if (sort === "name") items.sort((a, b) => name(a).localeCompare(name(b)));
      if (sort === "updated") items.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
      if (sort === "created") items.sort((a, b) => b.created_at.localeCompare(a.created_at));
      return [200, { items, next_cursor: null, urls_expire_at: "2026-09-01T11:00:00Z" }];
    }
    if (route === "POST /boxes") {
      if (boxes.has(body.number)) return [409, { error: "A box with this number already exists" }];
      const box = { number: body.number, description: body.description, contents: body.contents, photo: null, hidden: "", history: [] };
      boxes.set(box.number, box);
      history.set(box.number, body.contents ? [{ at: "2026-09-02T10:00:00Z", by: ADMIN.email, before: "", after: body.contents, note: "Создание коробки" }] : []);
      return [201, card(box)];
    }
    if (method === "DELETE" && histEntry) {
      const [number, id] = [decodeURIComponent(histEntry[1]), decodeURIComponent(histEntry[2])];
      history.set(number, (history.get(number) || []).filter((entry) => entry.id !== id));
      return [200, { deleted: id }];
    }
    if (method === "GET" && hist) return [200, { items: history.get(decodeURIComponent(hist[1])) || [] }];
    if (method === "PATCH" && one) {
      const box = boxes.get(decodeURIComponent(one[1]));
      Object.assign(box, { description: body.description, contents: body.contents, updated: "2026-10-10T12:00:00Z" });
      return [200, card(box)];
    }
    if (method === "GET" && one) {
      const box = boxes.get(decodeURIComponent(one[1]));
      return box ? [200, card(box)] : [404, { error: "Box not found" }];
    }
    switch (route) {
      case "GET /settings":
        return [200, { recognition: false, grok_key_set: false, grok_key_hint: "", grok_model: "" }];
      case "GET /settings/recognition-log":
        return [200, { items: [
          { at: "2026-10-10T10:00:00Z", user: ADMIN.email, number: "2", status: "error", message: "The recognition service did not accept the photo", detail: "HTTP 400: {\"error\": \"Model not found\"}", model: "grok-4.7", duration_ms: 1234, chars: 0 },
          { at: "2026-10-10T09:00:00Z", user: ADMIN.email, number: "1", status: "ok", message: "", detail: "", model: "grok-4.7", duration_ms: 5100, chars: 120 },
        ] }];
      case "GET /admin/recognition-log":
        return [200, { items: [
          { at: "2026-10-10T11:00:00Z", user: "guest@example.com", number: "3", status: "lost", message: "Recognition did not finish", detail: "", model: "grok-4.7", duration_ms: 0, chars: 0 },
        ] }];
      case "GET /me":
        return [200, { email: ADMIN.email, admin: true }];
      case "GET /version":
        return [200, {
          version: "v1.3.0",
          repository: "Tozapid/inventory-serverless-aws",
          url: "https://github.com/Tozapid/inventory-serverless-aws/releases",
          latest: "v1.4.0",
          newer: [{ version: "v1.4.0", title: "v1.4.0 — 2026-10-12", date: "2026-10-12", url: "https://github.com/Tozapid/inventory-serverless-aws/releases/tag/v1.4.0", notes: "Added\n• Settings → Version" }],
          checked_at: "2026-10-12T12:00:00Z",
          error: null,
        }];
      case "GET /admin/users":
        return [200, { items: users }];
      case "POST /admin/users":
        users.push({ email: body.email, status: "FORCE_CHANGE_PASSWORD", enabled: true, admin: !!body.admin, mfa: false, self: false });
        return [200, { email: body.email, temporary_password: "Temp-pass-1234", emailed: false }];
      case "PUT /admin/users":
        if (body.reset_mfa) users.find((user) => user.email === body.email).mfa = false;
        return [200, { email: body.email }];
      default:
        return [404, { error: "Not found" }];
    }
  }

  return { boxes, calls, handle, routes: (route) => calls.filter((call) => call.route === route) };
}

const executablePath = chromePath();
let browser;

test.before(async () => {
  if (executablePath) browser = await puppeteer.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
});

test.after(async () => {
  if (browser) await browser.close();
});

// A fresh browser profile and a fresh API for each test.
async function openInventory(t, { signedIn = true, lang = "en", totp = false, recognition = false, at = "/", api = fakeApi() } = {}) {
  if (!executablePath) {
    if (process.env.CI) throw new Error("Chrome not found; set CHROME_PATH");
    t.skip("Chrome not found; set CHROME_PATH to run the interface tests");
    return null;
  }
  const context = await browser.createBrowserContext();
  t.after(() => context.close());
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("dialog", (dialog) => dialog.accept());
  await page.setViewport({ width: 1280, height: 900 });
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const url = new URL(request.url());
    const reply = (status, contentType, body) => request.respond({ status, contentType, body });
    if (url.hostname === "cdn.jsdelivr.net" && url.pathname.includes("amazon-cognito-identity")) return reply(200, "text/javascript", COGNITO);
    // The real file from npm, so the page's integrity hash is checked too.
    if (url.hostname === "cdn.jsdelivr.net" && url.pathname.startsWith("/npm/qrcode-generator@")) {
      return request.respond({ status: 200, contentType: "text/javascript", headers: { "Access-Control-Allow-Origin": "*" }, body: QRCODE });
    }
    if (url.origin !== ORIGIN) return request.abort();
    if (url.pathname.startsWith("/api/")) {
      const body = request.postData() ? JSON.parse(request.postData()) : null;
      const [status, data] = api.handle(request.method(), url, body);
      return reply(status, "application/json", JSON.stringify(data));
    }
    if (url.pathname === "/config.js") return reply(200, "text/javascript", config(recognition));
    if (url.pathname.startsWith("/media/seed/")) {
      const photo = path.join(SEED, url.pathname.slice("/media/seed/".length));
      return fs.existsSync(photo) ? reply(200, "image/jpeg", fs.readFileSync(photo)) : reply(403, "text/plain", "denied");
    }
    const page = url.pathname === "/" || url.pathname.startsWith("/box") ? "index.html" : path.normalize(url.pathname);
    const file = path.join(WEB, page);
    if (!file.startsWith(WEB) || !fs.existsSync(file)) return reply(404, "text/plain", "not found");
    const headers = path.extname(file) === ".html" ? { "Content-Security-Policy": CSP } : {};
    return request.respond({ status: 200, contentType: TYPES[path.extname(file)] || "application/octet-stream", headers, body: fs.readFileSync(file) });
  });
  await page.evaluateOnNewDocument((lang, signedIn, totp) => {
    if (sessionStorage.getItem("test-ready")) return;
    sessionStorage.setItem("test-ready", "1");
    if (lang) localStorage.setItem("inventory-lang", lang);
    if (signedIn) localStorage.setItem("test-session", "1");
    if (totp) localStorage.setItem("test-totp", "1");
  }, lang, signedIn, totp);
  await page.goto(ORIGIN + at);
  t.after(() => assert.deepEqual(errors, [], "errors on the page"));
  return { page, api };
}

const text = (page, selector) => page.$eval(selector, (node) => node.textContent);
const rowNumbers = (page) => page.$$eval(".box-row .plate", (nodes) => nodes.map((node) => node.textContent));

async function until(check, message) {
  const end = Date.now() + 5000;
  while (Date.now() < end) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.fail("timed out: " + message);
}

// Clicks the first element matching the selector whose text is exactly `label`.
async function clickText(page, selector, label) {
  await page.waitForFunction((selector, label) => [...document.querySelectorAll(selector)].some((node) => node.textContent.trim() === label), {}, selector, label);
  await page.evaluate((selector, label) => [...document.querySelectorAll(selector)].find((node) => node.textContent.trim() === label).click(), selector, label);
}

async function openSettings(page, tab) {
  await page.waitForSelector(".box-row");
  await clickText(page, "button", "Settings");
  await clickText(page, ".dialog [role=tab]", tab);
}

test("sign in with the demo account", async (t) => {
  const wiki = await openInventory(t, { signedIn: false });
  if (!wiki) return;
  const { page } = wiki;
  await page.waitForSelector(".login form");
  assert.equal(await page.$eval('input[type="email"]', (node) => node.value), ADMIN.email);
  assert.match(await text(page, ".demo-note"), /admin@example\.com \/ admin123/);

  await page.$eval('input[type="password"]', (node) => { node.value = "wrong"; });
  await page.click(".login button.primary");
  await until(async () => (await text(page, ".login .error")) === "Wrong email or password", "wrong password message");

  await page.$eval('input[type="password"]', (node, password) => { node.value = password; }, ADMIN.password);
  await page.click(".login button.primary");
  await page.waitForSelector(".box-row");
  assert.deepEqual(await rowNumbers(page), ["1", "2", "3", "4", "5", "12", "A-7"]);
});

test("the example photos load and a card opens by its address", async (t) => {
  const wiki = await openInventory(t, { at: "/box/2" });
  if (!wiki) return;
  const { page } = wiki;
  await page.waitForSelector(".detail img");
  await until(() => page.$eval(".detail img", (img) => img.complete && img.naturalWidth > 0), "card photo");
  assert.match(await text(page, ".detail"), /Toolbox/);
  assert.match(await text(page, ".detail"), /Combination wrenches/);
  assert.match(await text(page, ".detail"), /Box created/, "the note the API writes is translated");
  assert.equal(await page.evaluate(() => document.title), "Box 2 — Inventory");
  const thumbs = await page.$$eval(".box-row img", (imgs) => imgs.length);
  assert.equal(thumbs, 4, "four example boxes have photos");
});

test("search finds a box by what is in its photo", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page, api } = wiki;
  await page.waitForSelector(".box-row");
  // "spanners" is only in the text of the toolbox photo, not in its card.
  await page.type(".search input[type=search]", "spanners");
  await until(async () => JSON.stringify(await rowNumbers(page)) === JSON.stringify(["2"]), "only the toolbox");
  assert.equal(api.routes("GET /boxes").at(-1).query.q, "spanners");
});

test("search looks through the history only when asked", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page, api } = wiki;
  await page.waitForSelector(".box-row");
  assert.equal(await page.$eval(".search-history input", (node) => node.checked), false, "off by default");
  await page.type(".search input[type=search]", "added");
  await page.waitForSelector(".empty");
  assert.equal(api.routes("GET /boxes").at(-1).query.history, undefined);

  await page.click(".search-history input");
  await until(async () => JSON.stringify(await rowNumbers(page)) === JSON.stringify(["1"]), "the box whose history mentions it");
  assert.equal(api.routes("GET /boxes").at(-1).query.history, "1");
  assert.equal(await text(page, ".box-row .history-match"), "found in history");
});

test("an admin deletes a history entry", async (t) => {
  const wiki = await openInventory(t, { at: "/box/1" });
  if (!wiki) return;
  const { page, api } = wiki;
  await page.waitForSelector(".history .event");
  assert.equal((await page.$$(".history .event")).length, 2);
  await page.click(".history .event .event-head button");
  await until(async () => (await page.$$(".history .event")).length === 1, "entry removed");
  assert.equal(api.calls.filter((call) => call.route.startsWith("DELETE /boxes/1/history/")).length, 1);
  assert.match(await text(page, ".history"), /Box created/, "the older entry stays");
  await page.reload();
  await page.waitForSelector(".history .event");
  assert.equal((await page.$$(".history .event")).length, 1);
});

test("the recognition log shows what Grok answered", async (t) => {
  const wiki = await openInventory(t, { recognition: true });
  if (!wiki) return;
  const { page } = wiki;
  await openSettings(page, "Recognition");
  await page.waitForSelector(".recognition-log .log-entry");
  const rows = await page.$$eval(".recognition-log .log-entry", (nodes) => nodes.map((node) => node.textContent));
  assert.equal(rows.length, 2);
  assert.match(rows[0], /Error/);
  assert.match(rows[0], /did not accept the photo/);
  assert.match(rows[0], /grok-4\.7 · 1\.2 s/);
  assert.match(rows[1], /Done/);
  assert.match(rows[1], /120 characters/);
  assert.match(await text(page, ".recognition-log pre"), /Model not found/);
  await page.click(".recognition-log a.plate");
  await page.waitForFunction(() => !document.querySelector(".dialog-wrap") && location.pathname === "/box/2");
});

test("an admin switches to everyone's recognition log", async (t) => {
  const wiki = await openInventory(t, { recognition: true });
  if (!wiki) return;
  const { page, api } = wiki;
  await openSettings(page, "Recognition");
  await page.waitForSelector(".recognition-log .log-entry");
  await clickText(page, ".log-scope button", "All users");
  await until(async () => (await page.$$(".recognition-log .log-entry")).length === 1, "everyone's log");
  const row = await text(page, ".recognition-log .log-entry");
  assert.match(row, /Did not finish/);
  assert.match(row, /guest@example\.com/);
  assert.equal(api.routes("GET /admin/recognition-log").length, 1);
  await clickText(page, ".log-scope button", "Mine");
  await until(async () => (await page.$$(".recognition-log .log-entry")).length === 2, "back to own log");
});

test("sorting is by index first, and the choice is kept", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page, api } = wiki;
  await page.waitForSelector(".box-row");
  assert.equal(await page.$eval(".sort-select", (node) => node.value), "number");
  assert.deepEqual(await page.$$eval(".sort-select option", (nodes) => nodes.map((node) => node.textContent)),
    ["By index", "By name", "Recently changed first", "Newest first"]);
  assert.deepEqual(await rowNumbers(page), ["1", "2", "3", "4", "5", "12", "A-7"]);
  assert.equal(api.routes("GET /boxes").at(-1).query.sort, undefined);

  await page.select(".sort-select", "name");
  await until(async () => (await rowNumbers(page))[0] === "12", "by name: Cables and chargers first");
  assert.equal(api.routes("GET /boxes").at(-1).query.sort, "name");

  await page.select(".sort-select", "created");
  await until(async () => (await rowNumbers(page))[0] === "A-7", "newest first");
  assert.equal(api.routes("GET /boxes").at(-1).query.sort, "created");
  assert.match(await text(page, ".box-row"), /Added /);

  await page.reload();
  await page.waitForSelector(".box-row");
  assert.equal(await page.$eval(".sort-select", (node) => node.value), "created", "kept after a reload");
  assert.equal(api.routes("GET /boxes").at(-1).query.sort, "created");

  await page.select(".sort-select", "updated");
  await until(async () => api.routes("GET /boxes").at(-1).query.sort === "updated", "changed sort");
  await page.goto(ORIGIN + "/box/3");
  await page.waitForSelector(".detail");
  await clickText(page, "#detail button", "Edit");
  await page.waitForSelector("#detail form.stack textarea");
  await page.$eval("#detail form.stack textarea", (node) => { node.value = "Edited"; });
  await clickText(page, "#detail form.stack button", "Save");
  await until(async () => (await rowNumbers(page))[0] === "3", "the edited box comes first");
  assert.equal(await page.$eval(".sort-select", (node) => node.value), "updated", "kept after editing");
});

test("a row shows the number and the name, the rest of the description below", async (t) => {
  const api = fakeApi();
  api.boxes.get("1").description = "Electronics: **Raspberry Pi** kit\nShelf 2, top left";
  api.boxes.get("5").description = "";
  const wiki = await openInventory(t, { api });
  if (!wiki) return;
  const { page } = wiki;
  await page.waitForSelector(".box-row");
  const rows = await page.$$eval(".box-row .box-copy", (nodes) => nodes.map((node) => ({
    title: node.querySelector(".box-title").textContent,
    rest: node.querySelector(".md-compact")?.textContent.trim() || "",
  })));
  assert.deepEqual(rows[0], { title: "1Electronics: Raspberry Pi kit", rest: "Shelf 2, top left" });
  assert.deepEqual(rows[1], { title: "2Toolbox", rest: "" }, "a one-line description has nothing below");
  assert.deepEqual(rows[4], { title: "5No description", rest: "" });
});

test("the photo view follows the list order row by row", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page } = wiki;
  await page.waitForSelector(".box-row");
  await clickText(page, ".seg button", "Photos");
  await page.waitForSelector(".gallery .tile");
  // Tiles as the eye reads them: top to bottom, then left to right.
  const seen = () => page.$$eval(".gallery .tile", (tiles) => tiles
    .map((tile) => ({ number: tile.querySelector(".plate").textContent, box: tile.getBoundingClientRect() }))
    .sort((a, b) => Math.round(a.box.top) - Math.round(b.box.top) || a.box.left - b.box.left)
    .map((tile) => tile.number));
  assert.deepEqual(await seen(), ["1", "2", "3", "4", "5", "12", "A-7"]);
  const tops = await page.$$eval(".gallery .tile", (tiles) => tiles.slice(0, 3).map((tile) => Math.round(tile.getBoundingClientRect().top)));
  assert.equal(new Set(tops).size, 1, "the first three tiles share a row");
  await page.setViewport({ width: 390, height: 844 });
  assert.deepEqual(await seen(), ["1", "2", "3", "4", "5", "12", "A-7"]);
});

test("keyboard: I edits, ⌘Enter saves, J and L move through the list", async (t) => {
  const wiki = await openInventory(t, { at: "/box/1" });
  if (!wiki) return;
  const { page, api } = wiki;
  await page.waitForSelector(".box-row.selected");
  await page.waitForSelector("#detail .detail-tools");
  const path = () => page.evaluate(() => location.pathname);

  await page.keyboard.press("KeyL");
  await until(async () => (await path()) === "/box/2", "L opens the next box");
  await page.keyboard.press("KeyL");
  await until(async () => (await path()) === "/box/3", "and the one after");
  await page.keyboard.press("KeyJ");
  await until(async () => (await path()) === "/box/2", "J goes back");
  await page.waitForFunction(() => document.querySelector("#detail .plate")?.textContent === "2");

  await page.keyboard.press("KeyI");
  await page.waitForSelector("#detail form.stack textarea");
  assert.equal(await page.evaluate(() => document.activeElement.tagName), "TEXTAREA", "the description is focused");
  await page.keyboard.press("KeyL");
  assert.equal(await path(), "/box/2", "letters type while editing");
  await page.keyboard.down("Meta");
  await page.keyboard.press("Enter");
  await page.keyboard.up("Meta");
  await until(() => api.routes("PATCH /boxes/2").length === 1, "⌘Enter saves");
  await until(async () => !(await page.$("#detail form.stack")), "the card leaves editing");
  assert.match(api.routes("PATCH /boxes/2")[0].body.description, /l/);

  await page.click(".search input[type=search]");
  await page.keyboard.press("KeyL");
  assert.equal(await path(), "/box/2", "letters in the search field stay letters");
});

test("keyboard: Esc leaves editing from a field, asking if the text changed", async (t) => {
  const wiki = await openInventory(t, { at: "/box/2" });
  if (!wiki) return;
  const { page, api } = wiki;
  const asked = [];
  page.on("dialog", (dialog) => asked.push(dialog.message()));
  await page.waitForSelector("#detail .detail-tools");

  await page.keyboard.press("KeyI");
  await page.waitForSelector("#detail form.stack textarea");
  await page.keyboard.press("Escape");
  await until(async () => !(await page.$("#detail form.stack")), "no changes: Esc leaves at once");
  assert.deepEqual(asked, []);

  await page.keyboard.press("KeyI");
  await page.waitForSelector("#detail form.stack textarea");
  await page.keyboard.type(" and more");
  await page.keyboard.press("Escape");
  await until(async () => !(await page.$("#detail form.stack")), "changes: asked, then left");
  assert.deepEqual(asked, ["Discard unsaved changes?"]);
  assert.equal(api.routes("PATCH /boxes/2").length, 0, "nothing saved");

  await page.keyboard.press("Escape");
  await until(async () => (await page.evaluate(() => location.pathname)) === "/", "the next Esc closes the card");

  await clickText(page, "button", "New box");
  await page.waitForSelector("form.composer input");
  await page.keyboard.press("Escape");
  assert.ok(await page.$("form.composer"), "the empty form stays until focused");
  await page.type("form.composer input", "31");
  await page.keyboard.press("Escape");
  await until(async () => !(await page.$("form.composer")), "Esc closes the new box form");
  assert.deepEqual(asked, ["Discard unsaved changes?", "Discard unsaved changes?"]);
});

test("keyboard: Ctrl+Enter creates a new box", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page, api } = wiki;
  await page.waitForSelector(".box-row");
  await clickText(page, "button", "New box");
  await page.waitForSelector("form.composer input");
  await page.type("form.composer input", "30");
  await page.keyboard.down("Control");
  await page.keyboard.press("Enter");
  await page.keyboard.up("Control");
  await until(() => api.boxes.has("30"), "box created");
});

test("a new box is created and opened", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page, api } = wiki;
  await page.waitForSelector(".box-row");
  await clickText(page, "button", "New box");
  await page.waitForSelector("form.composer input");
  await page.type("form.composer input", "20");
  const [description, contents] = await page.$$("form.composer textarea");
  await description.type("Garden hoses");
  await contents.type("- 25 m hose\n- Spray gun");
  await clickText(page, "form.composer button", "Create");
  await until(() => api.boxes.has("20"), "box saved");
  assert.deepEqual(api.routes("POST /boxes")[0].body, { number: "20", description: "Garden hoses", contents: "- 25 m hose\n- Spray gun" });
  await until(async () => (await rowNumbers(page)).includes("20"), "listed");
});

test("sign in asks for the authenticator code", async (t) => {
  const wiki = await openInventory(t, { signedIn: false, totp: true });
  if (!wiki) return;
  const { page } = wiki;
  await page.waitForSelector(".login form");
  await page.click(".login button.primary");
  await page.waitForSelector('.login input[autocomplete="one-time-code"]');
  assert.equal(await text(page, ".login h1"), "Code from the app");
  await page.type('.login input[autocomplete="one-time-code"]', "000000");
  await page.click(".login button.primary");
  await until(async () => (await text(page, ".login .error")) === "Wrong code", "wrong code message");
  await page.type('.login input[autocomplete="one-time-code"]', TOTP_CODE);
  await page.click(".login button.primary");
  await page.waitForSelector(".box-row");
});

test("the new password is typed twice", async (t) => {
  const opened = await openInventory(t);
  if (!opened) return;
  const { page } = opened;
  await openSettings(page, "Password");
  await page.waitForSelector('.settings-body input[autocomplete="current-password"]');
  const [next, again] = await page.$$('.settings-body input[autocomplete="new-password"]');
  await page.type('.settings-body input[autocomplete="current-password"]', "old-Password-1");
  await next.type("new-Password-12");
  await again.type("new-Password-13");
  await clickText(page, ".settings-body button", "Change password");
  await until(async () => (await text(page, ".settings-body .error")) === "The new passwords do not match", "mismatch message");
  await again.click({ clickCount: 3 });
  await again.type("new-Password-12");
  await clickText(page, ".settings-body button", "Change password");
  await until(async () => (await text(page, ".toast")) === "Password changed", "changed message");
  assert.equal(await page.$eval('.settings-body input[autocomplete="current-password"]', (node) => node.value), "", "the form is cleared");
});

test("two-factor authentication is turned on and off in the settings", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page } = wiki;
  await openSettings(page, "Two-factor authentication");
  await page.waitForSelector(".totp-status");
  assert.match(await text(page, ".totp-status"), /^Off\./);

  await clickText(page, ".settings-body button", "Enable");
  await page.waitForSelector(".settings-body svg.qr path");
  assert.ok((await page.$eval(".settings-body svg.qr path", (node) => node.getAttribute("d").length)) > 1000, "the QR code has modules");
  assert.equal(await text(page, ".totp-secret"), "JBSW Y3DP EHPK 3PXP");
  await page.type('.settings-body input[autocomplete="one-time-code"]', "000000");
  await clickText(page, ".settings-body button", "Verify and turn on");
  await until(async () => (await text(page, ".settings-body .error")) === "Wrong code", "wrong code message");
  await page.type('.settings-body input[autocomplete="one-time-code"]', TOTP_CODE);
  await clickText(page, ".settings-body button", "Verify and turn on");
  await until(async () => /^on Signing in needs a code/.test(await text(page, ".totp-status")), "status after turning on");

  await clickText(page, ".settings-body button", "Turn off");
  await until(async () => /^Off\./.test(await text(page, ".totp-status")), "status after turning off");
});

test("recognition settings stay hidden while recognition is off", async (t) => {
  const off = await openInventory(t);
  if (!off) return;
  await openSettings(off.page, "Password");
  const tabs = await off.page.$$eval(".dialog [role=tab]", (nodes) => nodes.map((node) => node.textContent));
  assert.deepEqual(tabs, ["Password", "Two-factor authentication", "Language", "Labels", "Users", "Version"]);
  assert.equal(off.api.routes("GET /settings").length, 0);
});

test("recognition settings show when it is on", async (t) => {
  const on = await openInventory(t, { recognition: true });
  if (!on) return;
  await openSettings(on.page, "Recognition");
  await on.page.waitForSelector(".settings-body input[type=password]");
  assert.equal(on.api.routes("GET /settings").length, 1);
});

test("the version tab shows the releases that came after this version", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page } = wiki;
  await openSettings(page, "Version");
  await page.waitForSelector(".releases li");
  assert.equal(await text(page, ".version-facts"), "Versionv1.3.0RepositoryTozapid/inventory-serverless-aws");
  assert.equal(await page.$eval(".version-facts a", (a) => a.href), "https://github.com/Tozapid/inventory-serverless-aws/releases/tag/v1.3.0");
  assert.equal(await text(page, ".totp-status"), "newer out Released since this version: 1");
  assert.equal(await text(page, ".releases li strong"), "v1.4.0 — 2026-10-12");
  assert.equal(await page.$eval(".release-notes", (node) => node.textContent), "Added\n• Settings → Version");
});

test("labels: pick boxes and print A4 sheets of 30 × 50 mm", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page } = wiki;
  await page.evaluate(() => { window.print = () => { window.printed = (window.printed || 0) + 1; }; });
  await openSettings(page, "Labels");
  await page.waitForSelector(".label-choice .check");
  assert.equal((await page.$$(".label-choice .check")).length, 7);
  await clickText(page, ".settings-body button", "Clear all");
  assert.equal(await page.$eval(".settings-body button.primary", (node) => node.disabled), true);
  await page.click(".label-choice .check:nth-child(1) input");
  await page.click(".label-choice .check:nth-child(7) input");
  assert.equal(await text(page, ".settings-body button.primary"), "Print: 2");
  await page.click(".settings-body button.primary");
  assert.equal(await page.evaluate(() => window.printed), 1);
  const labels = await page.$$eval(".print-labels .label", (nodes) => nodes.map((node) => ({
    number: node.querySelector(".label-number").textContent,
    qr: node.querySelector("svg.qr path").getAttribute("d").length > 500,
  })));
  assert.deepEqual(labels, [{ number: "1", qr: true }, { number: "A-7", qr: true }]);

  // On paper: one A4 page, labels 30 mm wide and 50 mm tall; the app is hidden.
  await page.emulateMediaType("print");
  const sizes = await page.evaluate(() => {
    const mm = (px) => Math.round((px * 25.4) / 96 * 10) / 10;
    const box = document.querySelector(".print-labels .label").getBoundingClientRect();
    return { width: mm(box.width), height: mm(box.height), app: getComputedStyle(document.getElementById("root")).display };
  });
  assert.deepEqual(sizes, { width: 30, height: 50, app: "none" });
  if (process.env.LABEL_PDF) await page.pdf({ path: process.env.LABEL_PDF, preferCSSPageSize: true, printBackground: true });
});

test("labels: size, font and text are chosen and kept", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page } = wiki;
  await page.evaluate(() => { window.print = () => {}; });
  await openSettings(page, "Labels");
  await page.waitForSelector(".label-preview .label");
  assert.equal(await text(page, ".label-settings .hint"), "On an A4 sheet: 6 × 5 = 30");
  assert.equal(await text(page, ".label-preview .label-number"), "1");
  assert.equal(await page.$(".label-preview .label-name"), null, "no name by default");

  const [width, height] = await page.$$(".label-settings .label-size:first-child input");
  await width.click({ clickCount: 3 });
  await width.type("50");
  await height.click({ clickCount: 3 });
  await height.type("30");
  await page.select(".label-settings select", "serif");
  await clickText(page, ".label-show label", "Name");
  assert.equal(await text(page, ".label-settings .hint"), "On an A4 sheet: 4 × 9 = 36");
  assert.equal(await text(page, ".label-preview .label-name"), "Drives and storage");
  assert.ok(await page.$(".label-preview .label.wide"), "a wide label puts the text beside the code");

  await page.reload();
  await openSettings(page, "Labels");
  await page.waitForSelector(".label-preview .label");
  assert.deepEqual(await page.$$eval(".label-settings .label-size:first-child input", (nodes) => nodes.map((node) => node.value)), ["50", "30"], "kept in the browser");
  assert.equal(await page.$eval(".label-settings select", (node) => node.value), "serif");
  await page.evaluate(() => { window.print = () => {}; });
  await page.click(".settings-body button.primary");
  await page.emulateMediaType("print");
  const label = await page.evaluate(() => {
    const mm = (px) => Math.round((px * 25.4) / 96);
    const node = document.querySelector(".print-labels .label");
    const box = node.getBoundingClientRect();
    const number = node.querySelector(".label-number");
    return { width: mm(box.width), height: mm(box.height), number: mm(parseFloat(getComputedStyle(number).fontSize)) };
  });
  assert.deepEqual([label.width, label.height], [50, 30]);
  assert.ok(label.number >= 5, "the number is as large as fits: " + label.number + " mm");
});

test("labels: the QR code leaves white to cut along, sizes can be set", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page } = wiki;
  await page.evaluate(() => { window.print = () => {}; });
  await openSettings(page, "Labels");
  await page.waitForSelector(".label-preview .label");
  const printed = async () => {
    await page.click(".settings-body button.primary");
    await page.emulateMediaType("print");
    const sizes = await page.evaluate(() => {
      const mm = (px) => Math.round((px * 25.4) / 96 * 10) / 10;
      const label = document.querySelector(".print-labels .label").getBoundingClientRect();
      const qr = document.querySelector(".print-labels .label svg.qr").getBoundingClientRect();
      const number = document.querySelector(".print-labels .label-number");
      return { qr: mm(qr.width), left: mm(qr.left - label.left), top: mm(qr.top - label.top), number: mm(parseFloat(getComputedStyle(number).fontSize)) };
    });
    await page.emulateMediaType("screen");
    return sizes;
  };
  const auto = await printed();
  assert.deepEqual([auto.qr, auto.left, auto.top], [24, 3, 3], "80% of 30 mm, the same white above as beside");

  const [qr, font] = await page.$$(".label-settings .label-size:nth-of-type(2) input");
  await qr.type("18");
  await font.type("8");
  const chosen = await printed();
  assert.deepEqual([chosen.qr, chosen.number], [18, 8]);
});

test("users: invite without mail, 2FA status and reset", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page, api } = wiki;
  await openSettings(page, "Users");
  await page.waitForSelector("table.users tbody tr");
  const rows = () => page.$$eval("table.users tbody tr", (nodes) => nodes.map((node) => [...node.children].map((cell) => cell.textContent)));
  assert.deepEqual((await rows()).map((cells) => cells[2]), ["2FA: off", "2FA: on"]);
  await clickText(page, "table.users button", "Reset 2FA");
  await until(async () => (await rows())[1][2] === "2FA: off", "2FA reset");
  assert.deepEqual(api.routes("PUT /admin/users").map((call) => call.body), [{ email: "guest@example.com", reset_mfa: true }]);

  await page.type(".invite input[type=email]", "new@example.com");
  await clickText(page, ".invite button", "Invite");
  await page.waitForSelector(".secret-note code");
  assert.match(await text(page, ".secret-note"), /User new@example\.com created; no email was sent/);
  assert.equal(await text(page, ".secret-note code"), "Temp-pass-1234");
});

test("the interface follows the chosen language", async (t) => {
  const wiki = await openInventory(t, { signedIn: false, lang: "fr" });
  if (!wiki) return;
  const { page } = wiki;
  await page.waitForSelector(".login button.primary");
  assert.equal(await text(page, ".login button.primary"), "Se connecter");
  assert.equal(await page.evaluate(() => document.documentElement.lang), "fr");
  await page.select(".lang-select", "it");
  await page.waitForFunction(() => document.documentElement.lang === "it" && document.querySelector(".login button.primary"));
  assert.equal(await text(page, ".login button.primary"), "Accedi");
});
