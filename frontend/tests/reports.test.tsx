import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../src/api";
import { OperationsPage, ReportsPage } from "../src/features/monitoring";

afterEach(() => vi.restoreAllMocks());
it("shows the actual reconciliation time and mismatch reasons from the server contract", async () => {
  const at = "2026-09-25T16:10:00+09:00";
  vi.spyOn(api, "operations").mockResolvedValue({
    jobs: [],
    health: {},
    entries_enabled: false,
    reconciliations: [
      { at, status: "mismatch", issues: ["브로커 보유 수량이 다릅니다."] },
    ],
  });
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <OperationsPage />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("차이 발견")).toBeInTheDocument();
  expect(screen.getByText("브로커 보유 수량이 다릅니다.")).toBeInTheDocument();
  expect(
    screen.getByText(new Date(at).toLocaleString("ko-KR")),
  ).toBeInTheDocument();
});
it("keeps unverified costs unknown and separates incomplete observations from performance", async () => {
  vi.spyOn(api, "reports").mockResolvedValue({
    daily: [{ session: "2026-09-25", equity: "100000", fees: null }],
    monthly: [{ month: "2026-09", fees: "0", return_pct: 2.5 }],
    baseline: null,
    observation_complete: false,
    observation_reasons: ["unresolved_orders"],
  });
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <ReportsPage />
    </QueryClientProvider>,
  );
  expect(
    await screen.findByText("결과가 미확정인 주문이 남아 있습니다."),
  ).toBeInTheDocument();
  const tables = screen.getAllByRole("table");
  expect(within(tables[0]).getByText("미확인")).toBeInTheDocument();
  expect(within(tables[0]).queryByText("0원")).not.toBeInTheDocument();
  expect(within(tables[1]).getByText("0원")).toBeInTheDocument();
  expect(within(tables[1]).getByText("+2.50%")).toBeInTheDocument();
});
