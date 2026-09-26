import { expect, it, vi } from "vitest";
import { ApiError, request, won } from "../src/api";
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
