import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { WorkspaceProvider } from "../src/features/shared";
import { OrderTicket } from "../src/features/orders";
import type { ManagedOrder } from "../src/api";

const context = {
  mode: "local_simulation",
  account_id: "test",
  account_alias: "감사 계좌",
  broker_connected: false,
  live_orders_enabled: false,
  entries_enabled: true,
  readiness: [],
  server_time: "2026-09-27T00:00:00Z",
};
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
let order: ManagedOrder | null;
let calls: {
  path: string;
  method: string;
  body: Record<string, unknown> | null;
}[];
let submitResponse: () => Promise<Response>;
beforeEach(() => {
  order = null;
  calls = [];
  submitResponse = async () =>
    response({
      ...order,
      state: "filled",
      filled_quantity: 1,
      remaining_quantity: 0,
    });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options: RequestInit = {}) => {
      const path = String(input);
      const method = options.method ?? "GET";
      const body = options.body ? JSON.parse(String(options.body)) : null;
      calls.push({ path, method, body });
      if (path === "/api/context") return response(context);
      if (path.startsWith("/api/order-intents/by-client-id/"))
        return order
          ? response(order)
          : response({ detail: "아직 등록되지 않았습니다." }, 404);
      if (path === "/api/order-intents") {
        order = {
          id: "order-1",
          ...body,
          state: "registered",
          filled_quantity: 0,
          cancelled_quantity: 0,
          remaining_quantity: body.quantity,
          created_at: "2026-09-27T00:00:00Z",
          events: [],
          error: null,
        };
        return response(order, 201);
      }
      if (path.endsWith("/submit")) return submitResponse();
      throw new Error(`unexpected ${path}`);
    }),
  );
});
function view() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceProvider>
        <MemoryRouter>
          <OrderTicket symbol="0048K0" price="10000" />
        </MemoryRouter>
      </WorkspaceProvider>
    </QueryClientProvider>,
  );
}
it("A01 locks the immutable payload during submission and keeps an alphanumeric symbol", async () => {
  const user = userEvent.setup();
  let finish!: (r: Response) => void;
  submitResponse = () =>
    new Promise((resolve) => {
      finish = resolve;
    });
  view();
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "주문 내용 등록" }),
    ).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "주문 내용 등록" }));
  await user.click(
    await screen.findByRole("button", { name: "등록한 주문 전송" }),
  );
  expect(screen.getByRole("spinbutton", { name: "주문 수량" })).toBeDisabled();
  expect(calls.filter((c) => c.path === "/api/order-intents")).toHaveLength(1);
  expect(calls.find((c) => c.path === "/api/order-intents")!.body!.symbol).toBe(
    "0048K0",
  );
  expect(calls.filter((c) => c.path.endsWith("/submit"))).toHaveLength(1);
  finish(
    response({
      ...order,
      state: "filled",
      filled_quantity: 1,
      remaining_quantity: 0,
    }),
  );
  await screen.findByRole("button", { name: "별도 새 주문 입력" });
});
it("A01 recovers an ambiguous request after unmount without submitting a new order", async () => {
  const user = userEvent.setup();
  submitResponse = async () => {
    order = { ...order!, state: "unknown" };
    throw new TypeError("lost response");
  };
  const first = view();
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "주문 내용 등록" }),
    ).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "주문 내용 등록" }));
  await user.click(
    await screen.findByRole("button", { name: "등록한 주문 전송" }),
  );
  await screen.findByText(/전송 결과를 확인 중입니다/);
  const id = order!.client_request_id;
  first.unmount();
  view();
  await screen.findByText(/결과 확인 필요/);
  expect(
    screen.queryByRole("button", { name: "주문 내용 등록" }),
  ).not.toBeInTheDocument();
  expect(calls.filter((c) => c.path === "/api/order-intents")).toHaveLength(1);
  expect(calls.filter((c) => c.path.endsWith("/submit"))).toHaveLength(1);
  expect(
    localStorage.getItem("invest-intent:local_simulation:test:0048K0"),
  ).toBe(id);
  expect(localStorage.length).toBe(1);
});
it("keeps a queued registered order from being submitted again", async () => {
  const user = userEvent.setup();
  submitResponse = async () =>
    response({
      ...order,
      pending_commands: [{ id: "cmd", kind: "submit", state: "queued" }],
    });
  view();
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "주문 내용 등록" }),
    ).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "주문 내용 등록" }));
  await user.click(
    await screen.findByRole("button", { name: "등록한 주문 전송" }),
  );
  await screen.findByText(/전송 작업을 요청했습니다/);
  expect(
    screen.queryByRole("button", { name: "등록한 주문 전송" }),
  ).not.toBeInTheDocument();
});
