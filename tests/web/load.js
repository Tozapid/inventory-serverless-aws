// Loads web/i18n.js into a jsdom window, the way the page does, and returns
// that window.
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const WEB = path.join(__dirname, "..", "..", "web");
const SEED = path.join(__dirname, "..", "..", "seed");

function source(name) {
  return fs.readFileSync(path.join(WEB, name), "utf8");
}

function loadPage({ lang, languages } = {}) {
  const dom = new JSDOM("<!doctype html><body></body>", { url: "https://inventory.test/", runScripts: "outside-only" });
  const { window } = dom;
  if (lang) window.localStorage.setItem("inventory-lang", lang);
  if (languages) {
    Object.defineProperty(window.navigator, "languages", { value: languages });
    Object.defineProperty(window.navigator, "language", { value: languages[0] });
  }
  window.eval(source("i18n.js"));
  return window;
}

module.exports = { WEB, SEED, source, loadPage };
