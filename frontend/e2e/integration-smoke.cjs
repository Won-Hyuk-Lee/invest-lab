// Explicit opt-in smoke test against an isolated local_simulation API + worker.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

async function poll(read, accept, label, timeout = 90000) {
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
    throw new Error(
      "Set INVEST_INTEGRATION_URL to an isolated UI backed by a local_simulation API and worker.",
    );
  const target = new URL(baseURL);
  if (!["127.0.0.1", "localhost"].includes(target.hostname))
    throw new Error("Only an isolated loopback target is allowed.");
  const contextInfo = await (await fetch(`${baseURL}/api/context`)).json();
  assert.equal(contextInfo.mode, "local_simulation");
  assert.equal(contextInfo.live_orders_enabled, false);
  assert.equal(
    contextInfo.readiness.find((r) => r.name === "worker")?.ready,
    true,
  );
  const output = path.resolve("test-results/integration");
  fs.mkdirSync(output, { recursive: true });
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
  const account = async () => (await fetch(`${baseURL}/api/portfolio`)).json();
  try {
    const before = await account();
    const beforeQuantity =
      before.positions.find((p) => p.symbol === "005930")?.quantity ?? 0;
    await page.goto(`${baseURL}/market/005930`);
    await page
      .getByRole("button", { name: "주문 내용 등록", exact: true })
      .click();
    await page
      .getByRole("button", { name: "등록한 주문 전송", exact: true })
      .click();
    await page
      .getByText(/삼성|005930/)
      .first()
      .waitFor();
    await page
      .getByText(/005930 · 매수 · 체결 완료/)
      .waitFor({ timeout: 60000 });
    await poll(
      account,
      (a) =>
        (a.positions.find((p) => p.symbol === "005930")?.quantity ?? 0) ===
        beforeQuantity + 1,
      "buy reflected in account",
    );
    await page.getByRole("link", { name: "주문 상세·체결 확인" }).click();
    await page
      .getByRole("heading", { name: "주문 상세", exact: true })
      .waitFor();
    report.buy = "filled and position increased by one";
    await page.goto(`${baseURL}/market/005930`);
    await page
      .getByRole("button", { name: "별도 새 주문 입력", exact: true })
      .click();
    await page.getByLabel("매매 구분", { exact: true }).selectOption("sell");
    await page
      .getByRole("button", { name: "주문 내용 등록", exact: true })
      .click();
    await page
      .getByRole("button", { name: "등록한 주문 전송", exact: true })
      .click();
    await page
      .getByText(/005930 · 매도 · 체결 완료/)
      .waitFor({ timeout: 60000 });
    await poll(
      account,
      (a) =>
        (a.positions.find((p) => p.symbol === "005930")?.quantity ?? 0) ===
        beforeQuantity,
      "sell reflected in account",
    );
    report.sell = "filled and position returned to baseline";
    await page.goto(`${baseURL}/orders`);
    await page.getByRole("heading", { name: "주문과 체결" }).waitFor();
    await page.screenshot({
      path: path.join(output, "orders.png"),
      fullPage: true,
    });
    await page.goto(`${baseURL}/research`);
    const datasetsBefore = (
      await (await fetch(`${baseURL}/api/research`)).json()
    ).datasets;
    if (!datasetsBefore.length) {
      await page.getByText("데이터 가져오기", { exact: true }).click();
      await page
        .getByRole("button", { name: "명시적으로 연습 데이터 생성" })
        .click();
    }
    const research = await poll(
      async () => (await fetch(`${baseURL}/api/research`)).json(),
      (r) => r.datasets.length > 0,
      "synthetic research dataset",
    );
    const dataset = research.datasets.find(
      (d) => d.source_kind === "synthetic",
    );
    assert.ok(
      dataset,
      "This smoke test only trains an explicitly synthetic dataset",
    );
    report.dataset = dataset.id;
    await page.reload();
    await page.getByRole("tab", { name: "피처", exact: true }).click();
    await page.locator(".table-scroll tbody tr").first().waitFor();
    report.features = "rendered from actual service";
    await page.getByRole("tab", { name: "백테스트·비교", exact: true }).click();
    await page.getByLabel("데이터셋", { exact: true }).selectOption(dataset.id);
    await page.getByRole("button", { name: "실험 실행", exact: true }).click();
    await page
      .getByRole("heading", { name: "전략별 비용 후 성과", exact: true })
      .waitFor({ timeout: 120000 });
    report.backtest = "job result and equity curve rendered";
    await page.screenshot({
      path: path.join(output, "backtest.png"),
      fullPage: true,
    });
    await page.getByRole("tab", { name: "통계 모델", exact: true }).click();
    await page.getByLabel("데이터셋", { exact: true }).selectOption(dataset.id);
    await page
      .getByRole("button", { name: "학습·검증 실행", exact: true })
      .click();
    await page
      .getByRole("heading", { name: "모델의 미사용 구간 평가", exact: true })
      .waitFor({ timeout: 180000 });
    report.training = "Ridge holdout metrics and lineage rendered";
    await page.screenshot({
      path: path.join(output, "training.png"),
      fullPage: true,
    });
    await page.goto(`${baseURL}/operations`);
    await page
      .getByRole("heading", { name: "운영 상태", exact: true })
      .waitFor();
    await page
      .getByLabel("작업 종류", { exact: true })
      .selectOption("daily_pipeline");
    await page.getByRole("button", { name: "작업 요청", exact: true }).click();
    await poll(
      async () => (await fetch(`${baseURL}/api/reports`)).json(),
      (r) => r.daily.length > 0,
      "daily report",
    );
    await page.goto(`${baseURL}/reports`);
    await page.locator(".table-scroll tbody tr").first().waitFor();
    report.reports = "daily snapshot visible";
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
