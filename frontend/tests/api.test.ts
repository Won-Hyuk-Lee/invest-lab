import { expect, it, vi } from "vitest";
import { api, ApiError, request, won } from "../src/api";
it("preserves structured conflict actions and correlation IDs", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            code: "VERSION_CONFLICT",
            message: "초안을 보존하세요",
            correlation_id: "check-1",
            suggested_action: "reload_latest_preserve_draft",
          }),
          { status: 409 },
        ),
    ),
  );
  await expect(request("/strategies", {})).rejects.toMatchObject({
    status: 409,
    code: "VERSION_CONFLICT",
    correlationId: "check-1",
    suggestedAction: "reload_latest_preserve_draft",
  });
});
it("does not retry ambiguous writes", async () => {
  const fetcher = vi.fn(async () => {
    throw new TypeError("offline");
  });
  vi.stubGlobal("fetch", fetcher);
  await expect(request("/order-intents/id/submit", {})).rejects.toBeInstanceOf(
    ApiError,
  );
  expect(fetcher).toHaveBeenCalledTimes(1);
});
it("does not show missing or invalid amounts as zero", () => {
  expect(won(null)).toBe("—");
  expect(won("invalid")).toBe("—");
  expect(won("")).toBe("—");
  expect(won(" ")).toBe("—");
  expect(won("0")).toBe("0");
});
it("does not send response metadata back in strict trading control requests", async () => {
  const fetcher = vi.fn(async () => new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetcher);
  await api.tradingControl({
    version: 2,
    enabled: true,
    allowed_symbols: ["005930"],
    max_order_value: "100000",
    max_position_pct: 20,
    max_invested_pct: 50,
    ...{ updated_at: "response-only" },
  });
  const options = (
    fetcher.mock.calls[0] as unknown as [string, RequestInit]
  )[1];
  expect(JSON.parse(String(options.body))).toEqual({
    expected_version: 2,
    enabled: true,
    allowed_symbols: ["005930"],
    max_order_value: "100000",
    max_position_pct: 20,
    max_invested_pct: 50,
  });
});
