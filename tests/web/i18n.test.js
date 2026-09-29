// Translations in web/i18n.js: every interface string the page uses has an
// English, French and Italian version, and the language follows the system.
const test = require("node:test");
const assert = require("node:assert/strict");
const { source, loadPage } = require("./load");

const OTHER = ["en", "fr", "it"];
const CYRILLIC = /[А-Яа-яЁё]/;

// Russian strings passed to t(...) in the page code, the keys of the dictionaries.
function usedKeys() {
  const keys = new Set();
  const literal = '"((?:[^"\\\\\\n]|\\\\.)*)"';
  const patterns = [
    new RegExp("(?<![\\w.])t\\(\\s*" + literal, "g"),
    new RegExp("(?<![\\w.])t\\([^()\"\\n]*\\?\\s*" + literal + "\\s*:\\s*" + literal, "g"),
  ];
  const code = source("app.js");
  for (const pattern of patterns) {
    for (const m of code.matchAll(pattern)) {
      m.slice(1).filter(Boolean).forEach((key) => keys.add(JSON.parse('"' + key + '"')));
    }
  }
  return [...keys].filter((key) => CYRILLIC.test(key));
}

const placeholders = (text) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join(",");

test("the page code has strings to translate", () => {
  assert.ok(usedKeys().length > 140, "found " + usedKeys().length);
});

test("no Russian text reaches the page without t()", () => {
  const code = source("app.js");
  const bare = [...code.matchAll(/"((?:[^"\\\n]|\\.)*[А-Яа-яЁё](?:[^"\\\n]|\\.)*)"/g)]
    .filter((m) => !/(?<![\w.])t\(\s*$|\?\s*$|:\s*$/.test(code.slice(Math.max(0, m.index - 40), m.index)))
    .map((m) => m[1]);
  // The one exception is the note the API writes itself, compared before translating.
  assert.deepEqual(bare, ["Создание коробки"]);
});

for (const lang of OTHER) {
  test("every interface string is translated: " + lang, () => {
    const { InventoryI18n } = loadPage({ lang });
    assert.equal(InventoryI18n.lang, lang);
    const missing = usedKeys().filter((key) => CYRILLIC.test(InventoryI18n.t(key)));
    assert.deepEqual(missing, []);
  });

  test("translations keep the {placeholders}: " + lang, () => {
    const { InventoryI18n } = loadPage({ lang });
    const broken = usedKeys().filter((key) => placeholders(InventoryI18n.t(key)) !== placeholders(key));
    assert.deepEqual(broken, []);
  });
}

test("Russian is the text in the code", () => {
  const { InventoryI18n } = loadPage({ lang: "ru" });
  assert.equal(InventoryI18n.t("Войти"), "Войти");
  assert.equal(InventoryI18n.t("Коробка {number}", { number: 12 }), "Коробка 12");
});

test("values fill placeholders, unknown ones stay", () => {
  const { InventoryI18n } = loadPage({ lang: "en" });
  assert.equal(InventoryI18n.t("Коробка {number}", { number: "A-7" }), "Box A-7");
  assert.equal(InventoryI18n.t("Коробка {number}"), "Box {number}");
  assert.equal(InventoryI18n.t("Строка, которой нет"), "Строка, которой нет");
});

test("the system language is used unless one was chosen", () => {
  const pick = (languages, lang) => loadPage({ languages, lang }).InventoryI18n;
  assert.equal(pick(["fr-CA", "en-US"]).lang, "fr");
  assert.equal(pick(["de-DE", "it-IT"]).lang, "it");
  assert.equal(pick(["ru"]).lang, "ru");
  assert.equal(pick(["de-DE", "ja"]).lang, "en");
  assert.equal(pick(["de-DE"]).chosen(), false);
  const chosen = pick(["fr-FR"], "ru");
  assert.equal(chosen.lang, "ru");
  assert.equal(chosen.chosen(), true);
  assert.equal(pick(["fr-FR"], "xx").lang, "fr", "an unknown stored value is ignored");
});

test("the page element carries the language", () => {
  assert.equal(loadPage({ lang: "it" }).document.documentElement.lang, "it");
});
