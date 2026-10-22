// Read-only acceptance of the compiled frontend served by the Python application.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const baseURL = process.env.INVEST_STATIC_URL;
  if (!baseURL)
    throw new Error(
      "Set INVEST_STATIC_URL to an isolated loopback static/API server.",
    );
  const target = new URL(baseURL);
  assert.ok(["127.0.0.1", "localhost"].includes(target.hostname));
  const context = await (await fetch(`${baseURL}/api/context`)).json();
  assert.equal(context.mode, "local_simulation");
  assert.equal(context.live_orders_enabled, false);
  const browser = await chromium.launch({
    channel:
      process.env.PW_CHANNEL ??
      (process.platform === "win32" ? "msedge" : undefined),
    headless: true,
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
  });
  const errors = [],
    failures = [],
    mutations = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("requestfailed", (request) => {
    if (request.failure()?.errorText !== "net::ERR_ABORTED")
      failures.push(request.url());
  });
  page.on("request", (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method()))
      mutations.push(request.url());
  });
  try {
    for (const route of ["/", "/orders", "/operations", "/settings"]) {
      const response = await page.goto(baseURL + route);
      assert.equal(response.status(), 200, route);
      await page.locator("main h1, main h2").first().waitFor();
      await page.waitForLoadState("networkidle");
      assert.equal(await page.locator('[aria-busy="true"]').count(), 0, route);
      await page.reload();
      await page.locator("main h1, main h2").first().waitFor();
      await page.waitForLoadState("networkidle");
      assert.equal(await page.locator('[aria-busy="true"]').count(), 0, route);
      assert.equal(await page.locator(".error-box").count(), 0, route);
      assert.equal(
        await page.locator('script[type="module"][src^="/assets/"]').count(),
        1,
      );
      assert.equal(await page.locator('script[src*="@vite"]').count(), 0);
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(failures, []);
    assert.deepEqual(mutations, []);
    const output = path.resolve("test-results/static");
    fs.mkdirSync(output, { recursive: true });
    await page.screenshot({
      path: path.join(output, "operations.png"),
      fullPage: true,
    });
    console.log(
      "Compiled assets, four direct routes and reloads passed; no JavaScript errors or write requests.",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
