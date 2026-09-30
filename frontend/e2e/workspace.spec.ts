import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("WCAG AA checks cover dashboard, order input and expanded research forms", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await fixture(page);
  const violations: unknown[] = [];
  const scan = async (view: string) => {
    const result = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    violations.push(
      ...result.violations.map((v) => ({
        view,
        id: v.id,
        nodes: v.nodes.map((n) => ({
          target: n.target,
          message: n.failureSummary,
        })),
      })),
    );
  };
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "보유 포트폴리오" }),
  ).toBeVisible();
  await scan("dashboard");
  await page.goto("/market/0048K0");
  await expect(
    page.getByRole("button", { name: "주문 내용 등록", exact: true }),
  ).toBeVisible();
  await scan("order");
  await page.goto("/research");
  await page.getByText("데이터 가져오기", { exact: true }).click();
  await scan("dataset-import");
  await page.getByRole("tab", { name: "통계 모델", exact: true }).click();
  await scan("model-form");
  await page.goto("/strategies");
  await expect(page.getByLabel("전략 이름")).toBeVisible();
  await scan("strategy");
  expect(violations).toEqual([]);
});

async function fixture(page: Page) {
  let portfolioRelease: (() => void) | undefined;
  const state = {
    delayPortfolio: false,
    loseSubmit: false,
    registrations: 0,
    submissions: 0,
    strategy: {
      version: 1,
      name: "연구 전략",
      weights: { oversold: 30, pullback: 25, trend: 25, rebound: 20 },
      oversold_rsi: 30,
      overbought_rsi: 70,
      review_threshold: 65,
      max_position_pct: 20,
    },
    releasePortfolio: () => portfolioRelease?.(),
    orders: [] as Record<string, unknown>[],
  };
  const bars = Array.from({ length: 130 }, (_, i) => ({
    session: new Date(Date.UTC(2026, 0, i + 1)).toISOString().slice(0, 10),
    open: 10000 + i,
    high: 10200 + i,
    low: 9800 + i,
    close: 10000 + i,
    volume: 100000,
  }));
  const analysis = {
    symbol: "0048K0",
    as_of: "2026-09-25",
    history_count: 260,
    close: 10000,
    returns: { "1": 1, "7": 2, "30": 3, "90": 4 },
    moving_averages: { "90": 9500, "120": 9000 },
    distances: { "90": 5, "120": 10 },
    rsi: 45,
    down_streak: 2,
    volume_ratio: 1.2,
    tags: ["uptrend"],
    score: 70,
    components: [],
    strategy_version: 1,
  };
  const instrument = {
    symbol: "0048K0",
    name: "검증 ETF",
    kind: "etf_kr",
    sector: "지수",
    quote: {
      symbol: "0048K0",
      price: "10000",
      source: "fixture",
      observed_at: "2026-09-25T06:30:00Z",
    },
  };
  await page.route("**/api/**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname;
    const send = (body: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    if (path === "/api/context")
      return send({
        mode: "local_simulation",
        account_id: "fixture",
        account_alias: "검증 계좌",
        broker_connected: false,
        live_orders_enabled: false,
        entries_enabled: true,
        readiness: [],
        server_time: "2026-09-27T00:00:00Z",
      });
    if (path === "/api/trading")
      return send({
        mode: "local_simulation",
        execution_only: true,
        entries_enabled: true,
        control: {
          version: 1,
          enabled: false,
          allowed_symbols: [],
          max_order_value: "100000",
          max_position_pct: 20,
          max_invested_pct: 50,
        },
        worker: null,
        signals: [],
      });
    if (path === "/api/trading/instruments")
      return send([
        {
          symbol: instrument.symbol,
          name: instrument.name,
          kind: instrument.kind,
        },
      ]);
    if (path === "/api/portfolio") {
      if (state.delayPortfolio)
        await new Promise<void>((resolve) => {
          portfolioRelease = resolve;
        });
      return send({
        cash: "9000000",
        initial_cash: "10000000",
        equity: "10000000",
        holdings_value: "1000000",
        profit: "0",
        mode: "local_simulation",
        source: "fixture",
        orders: [],
        positions: [
          {
            symbol: "0048K0",
            name: "검증 ETF",
            quantity: 100,
            average_price: "10000",
            price: "10000",
            value: "1000000",
            profit: "0",
          },
        ],
      });
    }
    if (path === "/api/screener")
      return send({
        rows: [{ ...instrument, ...analysis }],
        source: "fixture",
        strategy: state.strategy,
        generated_at: "2026-09-27T00:00:00Z",
      });
    if (path === "/api/instruments/0048K0")
      return send({ ...instrument, analysis, bars, source: "fixture" });
    if (path === "/api/strategies") {
      if (req.method() === "POST") {
        const input = req.postDataJSON();
        if (input.expected_version !== state.strategy.version)
          return send(
            { code: "VERSION_CONFLICT", message: "다른 변경이 있습니다." },
            409,
          );
        state.strategy = { ...input, version: state.strategy.version + 1 };
        return send(state.strategy, 201);
      }
      return send({ versions: [state.strategy] });
    }
    if (path === "/api/deployment")
      return send({
        version: 1,
        enabled: false,
        strategy_type: "kospi_pullback",
        model_id: null,
        allocation_policy: "single_active",
        rules: {
          rsi_low: 35,
          rsi_high: 50,
          stop_atr: 2,
          max_positions: 5,
          max_position_pct: 10,
          use_quality: false,
          use_macro: false,
        },
        updated_at: "2026-09-27T00:00:00Z",
      });
    if (path === "/api/order-intents") {
      state.registrations++;
      const body = req.postDataJSON();
      const o = {
        ...body,
        id: "order-fixture",
        state: "registered",
        filled_quantity: 0,
        cancelled_quantity: 0,
        remaining_quantity: body.quantity,
        events: [],
        error: null,
        created_at: "2026-09-27T00:00:00Z",
      };
      state.orders.push(o);
      return send(o, 201);
    }
    if (path.endsWith("/submit")) {
      state.submissions++;
      state.orders[0].state = state.loseSubmit ? "unknown" : "filled";
      if (state.loseSubmit) return route.abort("failed");
      return send(state.orders[0]);
    }
    if (path.startsWith("/api/order-intents/by-client-id/"))
      return state.orders.length
        ? send(state.orders[0])
        : send({ detail: "not found" }, 404);
    if (path === "/api/orders") {
      const params = new URL(req.url()).searchParams;
      const offset = Number(params.get("cursor") || 0);
      const limit = Number(params.get("limit") || 20);
      const rows = state.orders.filter(
        (o) =>
          (!params.get("status") || o.state === params.get("status")) &&
          (!params.get("symbol") ||
            String(o.symbol).includes(params.get("symbol")!)),
      );
      return send({
        items: rows.slice(offset, offset + limit),
        total: rows.length,
        next_cursor:
          offset + limit < rows.length ? String(offset + limit) : null,
      });
    }
    if (path.startsWith("/api/orders/")) return send(state.orders[0]);
    if (path === "/api/research")
      return send({
        datasets: [],
        experiments: [],
        models: [],
        fundamentals: [],
        macro: [],
        features: [],
        readiness: [],
        real_validation_complete: false,
      });
    if (path === "/api/watchlist")
      return send({ items: [], as_of: null, source: "fixture" });
    if (path === "/api/operations")
      return send({
        jobs: [],
        reconciliations: [],
        health: { worker_running: false },
        entries_enabled: false,
      });
    if (path === "/api/reports") return send({ daily: [], monthly: [] });
    return send({ message: `Fixture does not implement ${path}` }, 404);
  });
  return state;
}

test("A08 waits for the account instead of claiming an empty cash-only account", async ({
  page,
}) => {
  const state = await fixture(page);
  state.delayPortfolio = true;
  await page.goto("/archive");
  await expect(
    page.getByText(
      "계좌를 조회하고 있습니다. 금액과 보유 상태는 조회 후 표시합니다.",
    ),
  ).toBeVisible();
  await expect(
    page.getByText("첫 번째 투자 판단을 기록해 보세요."),
  ).toHaveCount(0);
  state.releasePortfolio();
  await expect(
    page.getByRole("heading", { name: "나의 투자 현황" }),
  ).toBeVisible();
  await expect(page.getByText("100주", { exact: true })).toBeVisible();
});

test("A01 unknown submission survives a dialog close and browser reload without a new POST", async ({
  page,
}) => {
  const state = await fixture(page);
  state.loseSubmit = true;
  await page.goto("/market/0048K0");
  await page.getByRole("button", { name: "주문 내용 등록" }).click();
  await page.getByRole("button", { name: "등록한 주문 전송" }).click();
  await expect(page.getByText(/전송 결과를 확인 중입니다/)).toBeVisible();
  await expect(
    page.getByRole("spinbutton", { name: "주문 수량" }),
  ).toBeDisabled();
  await page.reload();
  await expect(
    page.getByText(/결과 확인 필요/, { exact: false }).first(),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "주문 내용 등록" }),
  ).toHaveCount(0);
  expect(state.registrations).toBe(1);
  expect(state.submissions).toBe(1);
  const stored = await page.evaluate(() => Object.values(localStorage));
  expect(stored).toEqual([state.orders[0].client_request_id]);
});

test("A07 refresh retains a conflicting strategy draft and reapplies edits", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto("/strategies");
  await page.getByLabel("전략 이름", { exact: true }).fill("보존할 초안");
  state.strategy = {
    ...state.strategy,
    version: 2,
    name: "서버 최신 전략",
    review_threshold: 75,
  };
  await page
    .getByRole("button", { name: "새 버전으로 저장", exact: true })
    .click();
  await expect(page.getByText(/내 초안은 유지했습니다/)).toBeVisible();
  await page
    .getByRole("button", { name: "최신 설정 조회", exact: true })
    .click();
  await expect(page.getByLabel("전략 이름", { exact: true })).toHaveValue(
    "보존할 초안",
  );
  await page.getByRole("button", { name: "내 변경을 최신값에 재적용" }).click();
  await expect(page.getByLabel("검토 기준 점수")).toHaveValue("75");
  await page
    .getByRole("button", { name: "새 버전으로 저장", exact: true })
    .click();
  await expect(page.getByText("전략 v3을 저장했습니다.")).toBeVisible();
});

test("A09 320px reflow and A10 alphanumeric navigation stay usable", async ({
  page,
}) => {
  await fixture(page);
  await page.setViewportSize({ width: 320, height: 780 });
  await page.goto("/market");
  await page.getByRole("textbox", { name: "종목 검색" }).fill("0048K0");
  await expect(
    page.getByRole("button", { name: "검증 ETF", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(320);
  await page.getByRole("button", { name: "검증 ETF", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(
    await page
      .getByRole("dialog")
      .evaluate((e) => e.scrollWidth <= e.clientWidth),
  ).toBe(true);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("research and operation routes present explicit empty data", async ({
  page,
}) => {
  await fixture(page);
  for (const [path, heading] of [
    ["/watchlist", "후보·보유 감시"],
    ["/orders", "주문과 체결"],
    ["/research", "연구실"],
    ["/reports", "성과·운용 보고"],
    ["/operations", "운영 상태"],
  ]) {
    await page.goto(path);
    await expect(
      page.getByRole("heading", { name: heading, exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("alert")).toHaveCount(0);
  }
});

test("orders over one hundred remain paginated with a distinct range total", async ({
  page,
}) => {
  const state = await fixture(page);
  state.orders = Array.from({ length: 105 }, (_, index) => ({
    id: `order-${index}`,
    client_request_id: `request-${index}`,
    symbol: String(index).padStart(6, "0"),
    side: "buy",
    quantity: 1,
    filled_quantity: 1,
    remaining_quantity: 0,
    cancelled_quantity: 0,
    state: "filled",
    order_type: "market",
    limit_price: null,
    created_at: "2026-09-27T00:00:00Z",
    events: [],
  }));
  await page.goto("/orders");
  await expect(
    page.getByText("조회 범위 전체 105건 · 현재 페이지 20건"),
  ).toBeVisible();
  await expect(page.locator("tbody tr")).toHaveCount(20);
  await expect(
    page.getByRole("cell", { name: "000000", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "다음 페이지" }).click();
  await expect(
    page.getByRole("cell", { name: "000020", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "000000", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "첫 페이지" }).click();
  await expect(
    page.getByRole("cell", { name: "000000", exact: true }),
  ).toBeVisible();
});
