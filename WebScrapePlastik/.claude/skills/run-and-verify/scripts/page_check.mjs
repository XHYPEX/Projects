// Open one page of the running app in headless Chromium, run optional JS on it, and report what happened.
//
//   node page_check.mjs --base http://127.0.0.1:8769 --route /sales-order [--eval "openPreorder('ID')"]
//        [--wait 1500] [--shot out.png] [--width 1500] [--height 950] [--user qa --pass qa-test-1234]
//        [--pdf out.pdf]   with --eval that starts a print (e.g. "svcDoPrint()"), saves what the printer would get
//
// Prints page errors, failing /api calls (status >= 400) and the visible text of the page's main area,
// so a check never rests on "should work". Exit code 1 when a page error happened.
import { chromium } from "playwright-core";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const base = arg("base", "http://127.0.0.1:8769"), route = arg("route", "/dashboard");
const user = arg("user", "qa"), pass = arg("pass", process.env.QA_PASSWORD || "qa-test-1234");
const exe = process.env.CHROME_PATH || (() => {
  const root = path.join(os.homedir(), "Library/Caches/ms-playwright");
  const dir = fs.existsSync(root) && fs.readdirSync(root).filter(d => /^chromium-\d+$/.test(d)).sort().pop();
  return dir ? path.join(root, dir, "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing") : undefined;
})();

const browser = await chromium.launch({ executablePath: exe });
const ctx = await browser.newContext({ viewport: { width: +arg("width", 1500), height: +arg("height", 950) }, deviceScaleFactor: +arg("scale", 1) });
const login = await ctx.request.post(`${base}/api/auth/login`, { data: { username: user, password: pass } });
if (!login.ok()) { console.log("LOGIN FAILED", login.status(), await login.text()); process.exit(2); }
const page = await ctx.newPage();
const errors = [], api = [];
page.on("pageerror", e => errors.push(e.message));
page.on("response", r => { if (r.url().includes("/api/") && r.status() >= 400) api.push(`${r.status()} ${r.request().method()} ${r.url().replace(base, "")}`); });
await page.goto(`${base}/#${route}`);
await page.waitForTimeout(+arg("wait", 1500));
const code = arg("eval"), pdf = arg("pdf");
// For a PDF the print dialog is skipped and the page rule / body class a print sets are left on until saved.
if (pdf) await page.evaluate(() => {
  window.print = () => {};
  window.printWithPageRule = (rule, cls) => { document.getElementById("print-page-rule").textContent = rule; if (cls) document.body.classList.add(cls); };
});
if (code) {
  try { console.log("eval result:", JSON.stringify(await page.evaluate(code))); } catch (e) { errors.push("eval: " + e.message); }
  await page.waitForTimeout(+arg("wait", 1500));
}
if (pdf) {
  await page.emulateMedia({ media: "print" });
  await page.pdf({ path: pdf, preferCSSPageSize: true, printBackground: true });
  await page.emulateMedia({ media: "screen" });
  console.log("pdf:", pdf, "(view: qlmanage -t -s 1600 -o <dir> " + pdf + ", then open the .png)");
}
const shot = arg("shot");
if (shot) { await page.screenshot({ path: shot, fullPage: arg("full") === "1" }); console.log("screenshot:", shot); }
console.log("hash:", await page.evaluate(() => location.hash));
console.log("title:", await page.evaluate(() => document.getElementById("topbar-title")?.textContent || ""));
console.log("page errors:", errors.length ? errors : "none");
console.log("failing api calls:", api.length ? api : "none");
await browser.close();
process.exit(errors.length ? 1 : 0);
