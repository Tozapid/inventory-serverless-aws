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
    updated_at: "2026-09-01T10:00:00Z",
    updated_by: ADMIN.email,
    version: 1,
    urls_expire_at: "2026-09-01T11:00:00Z",
  });
  seed.forEach((box) => {
    boxes.set(box.number, box);
    history.set(box.number, box.history.map((entry) => ({ at: "2026-09-01T10:00:00Z", by: ADMIN.email, before: entry.before, after: entry.after, note: entry.note })).reverse());
  });

  function handle(method, url, body) {
    const route = method + " " + url.pathname.replace(/^\/api/, "");
    calls.push({ route, query: Object.fromEntries(url.searchParams), body });
    const one = /^\/boxes\/([^/]+)$/.exec(url.pathname.replace(/^\/api/, ""));
    const hist = /^\/boxes\/([^/]+)\/history$/.exec(url.pathname.replace(/^\/api/, ""));
    if (route === "GET /boxes") {
      const words = fold(url.searchParams.get("q")).split(" ").filter(Boolean);
      const items = [...boxes.values()]
        .filter((box) => words.every((word) => fold([box.number, box.description, box.contents, box.hidden].join(" ")).includes(word)))
        .map(card);
      return [200, { items, next_cursor: null, urls_expire_at: "2026-09-01T11:00:00Z" }];
    }
    if (route === "POST /boxes") {
      if (boxes.has(body.number)) return [409, { error: "A box with this number already exists" }];
      const box = { number: body.number, description: body.description, contents: body.contents, photo: null, hidden: "", history: [] };
      boxes.set(box.number, box);
      history.set(box.number, body.contents ? [{ at: "2026-09-02T10:00:00Z", by: ADMIN.email, before: "", after: body.contents, note: "Создание коробки" }] : []);
      return [201, card(box)];
    }
    if (method === "GET" && hist) return [200, { items: history.get(decodeURIComponent(hist[1])) || [] }];
    if (method === "GET" && one) {
      const box = boxes.get(decodeURIComponent(one[1]));
      return box ? [200, card(box)] : [404, { error: "Box not found" }];
    }
    switch (route) {
      case "GET /settings":
        return [200, { recognition: false, grok_api_key: "", grok_model: "" }];
      case "GET /me":
        return [200, { email: ADMIN.email, admin: true }];
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
    return reply(200, TYPES[path.extname(file)] || "application/octet-stream", fs.readFileSync(file));
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
  assert.match(await text(page, ".detail"), /Small hand tools/);
  assert.match(await text(page, ".detail"), /Needle-nose pliers/);
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
  await page.type(".search input", "baubles");
  await until(async () => JSON.stringify(await rowNumbers(page)) === JSON.stringify(["3"]), "only the Christmas box");
  assert.equal(api.routes("GET /boxes").at(-1).query.q, "baubles");
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

test("two-step sign-in is turned on and off in the settings", async (t) => {
  const wiki = await openInventory(t);
  if (!wiki) return;
  const { page } = wiki;
  await openSettings(page, "Two-step sign-in");
  await page.waitForSelector(".totp-status");
  assert.match(await text(page, ".totp-status"), /^Off\./);

  await clickText(page, ".settings-body button", "Enable");
  await page.waitForSelector(".settings-body svg.qr path");
  assert.ok((await page.$eval(".settings-body svg.qr path", (node) => node.getAttribute("d").length)) > 1000, "the QR code has modules");
  assert.equal(await text(page, ".totp-secret"), "JBSW Y3DP EHPK 3PXP");
  await page.type('.settings-body input[autocomplete="one-time-code"]', "000000");
  await clickText(page, ".settings-body button", "Confirm");
  await until(async () => (await text(page, ".settings-body .error")) === "Wrong code", "wrong code message");
  await page.type('.settings-body input[autocomplete="one-time-code"]', TOTP_CODE);
  await clickText(page, ".settings-body button", "Confirm");
  await until(async () => /^On\./.test(await text(page, ".totp-status")), "status after turning on");

  await clickText(page, ".settings-body button", "Turn off");
  await until(async () => /^Off\./.test(await text(page, ".totp-status")), "status after turning off");
});

test("recognition settings stay hidden while recognition is off", async (t) => {
  const off = await openInventory(t);
  if (!off) return;
  await openSettings(off.page, "Password");
  const tabs = await off.page.$$eval(".dialog [role=tab]", (nodes) => nodes.map((node) => node.textContent));
  assert.deepEqual(tabs, ["Password", "Two-step sign-in", "Language", "Users"]);
  assert.equal(off.api.routes("GET /settings").length, 0);
});

test("recognition settings show when it is on", async (t) => {
  const on = await openInventory(t, { recognition: true });
  if (!on) return;
  await openSettings(on.page, "Recognition");
  await on.page.waitForSelector(".settings-body input[type=password]");
  assert.equal(on.api.routes("GET /settings").length, 1);
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
