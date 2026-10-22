// Explicit opt-in smoke against an isolated local_simulation API + worker.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");

async function poll(read, accept, label, timeout = 60000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const value = await read();
    if (accept(value)) return value;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`Timed out: ${label}`);
}

(async () => {
  const baseURL = process.env.INVEST_INTEGRATION_URL;
  if (!baseURL)
    throw new Error("Set INVEST_INTEGRATION_URL to an isolated API + worker.");
  assert.ok(["127.0.0.1", "localhost"].includes(new URL(baseURL).hostname));
  const read = async (route) => (await fetch(baseURL + route)).json();
  const contextInfo = await read("/api/context");
  assert.equal(contextInfo.mode, "local_simulation");
  assert.equal(contextInfo.live_orders_enabled, false);
  assert.equal(
    contextInfo.readiness.find((r) => r.name === "worker")?.ready,
    true,
  );
  assert.equal((await read("/api/trading")).execution_only, true);
  const output = path.resolve("test-results/integration");
  fs.mkdirSync(output, { recursive: true });
  const initialPolicy = (await read("/api/trading")).control;
  const pause = await fetch(`${baseURL}/api/trading/control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      expected_version: initialPolicy.version,
      enabled: false,
      allowed_symbols: initialPolicy.allowed_symbols,
      max_order_value: initialPolicy.max_order_value,
      max_position_pct: initialPolicy.max_position_pct,
      max_invested_pct: initialPolicy.max_invested_pct,
    }),
  });
  assert.equal(pause.status, 200);
  const browser = await chromium.launch({
    channel:
      process.env.PW_CHANNEL ??
      (process.platform === "win32" ? "msedge" : undefined),
    headless: true,
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const report = {};
  const account = () => read("/api/portfolio");
  const quantity = (a) =>
    a.positions.find((p) => p.symbol === "005930")?.quantity ?? 0;
  const makeSignal = (side, changes = {}) => ({
    client_request_id: randomUUID(),
    symbol: "005930",
    side,
    quantity: 1,
    order_type: "market",
    limit_price: null,
    source: "browser-smoke",
    reason: "격리된 로컬 실행 확인",
    expires_at: new Date(Date.now() + 120000).toISOString(),
    ...changes,
  });
  const inspect = async (signal) => {
    await page
      .getByLabel("판단 JSON", { exact: true })
      .fill(JSON.stringify(signal, null, 2));
    await page
      .getByRole("button", { name: "전송 없이 검사", exact: true })
      .click();
  };
  const submitAndFill = async (signal) => {
    await inspect(signal);
    await page.getByText(/검사 완료 · 예상 주문금액/).waitFor();
    await page
      .getByRole("button", { name: "검사한 판단으로 주문 요청", exact: true })
      .click();
    return poll(
      () => read(`/api/trading/signals/${signal.client_request_id}`),
      (o) => o.state === "filled",
      `${signal.side} fill`,
    );
  };
  try {
    const beforeQuantity = quantity(await account());
    await page.goto(baseURL);
    await page
      .getByRole("heading", { name: "로컬 매매 도구", exact: true })
      .waitFor();
    await page.getByLabel("허용 종목 코드", { exact: true }).fill("005930");
    await page
      .getByLabel("주문당 최대 금액 (원)", { exact: true })
      .fill("1000000");
    const version = (await read("/api/trading")).control.version;
    await page.getByRole("button", { name: "설정 저장", exact: true }).click();
    await poll(
      () => read("/api/trading"),
      (s) => s.control.version > version,
      "policy save",
    );
    await page.waitForLoadState("networkidle");
    await page
      .getByRole("button", {
        name: "저장하고 외부 자동 주문 허용",
        exact: true,
      })
      .click();
    await poll(
      () => read("/api/trading"),
      (s) => s.control.enabled,
      "external orders enabled",
    );
    await page.getByRole("heading", { name: "허용", exact: true }).waitFor();
    const buy = makeSignal("buy");
    const bought = await submitAndFill(buy);
    await poll(
      account,
      (a) => quantity(a) === beforeQuantity + 1,
      "buy position",
    );
    const duplicate = await fetch(`${baseURL}/api/trading/signals`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buy),
    });
    const duplicateBody = await duplicate.json();
    assert.equal(duplicate.status, 202, JSON.stringify(duplicateBody));
    assert.equal(duplicateBody.id, bought.id);
    report.buy = "JSON decision filled; same ID returned the same order";
    await page
      .getByRole("link", { name: "주문 상세 확인", exact: true })
      .click();
    await page
      .getByRole("heading", { name: "주문 상세", exact: true })
      .waitFor();
    await page.goto(baseURL);
    await page
      .getByRole("button", { name: "새 판단 예시 만들기", exact: true })
      .click();
    await submitAndFill(makeSignal("sell"));
    await poll(account, (a) => quantity(a) === beforeQuantity, "sell position");
    report.sell = "JSON sell filled; holdings returned to baseline";
    const count = (await read("/api/trading")).signals.length;
    await page.getByLabel("판단 JSON", { exact: true }).fill("{");
    await page
      .getByRole("button", { name: "전송 없이 검사", exact: true })
      .click();
    await page.locator(".error-box").first().waitFor();
    assert.equal(
      await page
        .getByRole("button", { name: "검사한 판단으로 주문 요청", exact: true })
        .isEnabled(),
      false,
    );
    await page
      .getByRole("button", { name: "새 판단 예시 만들기", exact: true })
      .click();
    await inspect(makeSignal("buy", { quantity: 1000 }));
    await page.getByText(/한 주문의 금액 한도를 초과합니다/).waitFor();
    assert.equal((await read("/api/trading")).signals.length, count);
    report.invalid = "Malformed JSON and amount limit prevented registration";
    await page
      .getByRole("button", { name: "외부 자동 주문 중지", exact: true })
      .click();
    await poll(
      () => read("/api/trading"),
      (s) => !s.control.enabled,
      "external stop",
    );
    await page
      .getByRole("button", { name: "새 판단 예시 만들기", exact: true })
      .click();
    await inspect(makeSignal("buy"));
    await page.getByText(/검사 완료 · 예상 주문금액/).waitFor();
    assert.equal(
      await page
        .getByRole("button", { name: "검사한 판단으로 주문 요청", exact: true })
        .isEnabled(),
      false,
    );
    report.stop = "Stopped policy permits preview but disables submission";
    await page.screenshot({
      path: path.join(output, "trading.png"),
      fullPage: true,
    });
    await page.goto(`${baseURL}/orders`);
    await page
      .getByRole("heading", { name: "주문과 체결", exact: true })
      .waitFor();
    await page.screenshot({
      path: path.join(output, "orders.png"),
      fullPage: true,
    });
    report.javascriptErrors = errors;
    assert.deepEqual(errors, []);
    fs.writeFileSync(
      path.join(output, "result.json"),
      JSON.stringify(report, null, 2),
    );
    console.log(JSON.stringify(report, null, 2));
  } catch (error) {
    await page.screenshot({
      path: path.join(output, "failure.png"),
      fullPage: true,
    });
    fs.writeFileSync(
      path.join(output, "failure.txt"),
      `${error.stack}\n\n${await page.locator("body").innerText()}`,
    );
    throw error;
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
