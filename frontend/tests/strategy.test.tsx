import { expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrategyEditor } from "../src/App";
import type { Strategy } from "../src/api";
const initial: Strategy = {
  version: 1,
  name: "첫 전략",
  weights: { oversold: 30, pullback: 25, trend: 25, rebound: 20 },
  oversold_rsi: 30,
  overbought_rsi: 70,
  review_threshold: 65,
  max_position_pct: 20,
};
it("A07 preserves a dirty draft on refresh and reapplies only changed fields", async () => {
  const user = userEvent.setup();
  const client = new QueryClient();
  const latest = {
    ...initial,
    version: 2,
    name: "다른 창 변경",
    review_threshold: 75,
  };
  const content = (strategy: Strategy) => (
    <QueryClientProvider client={client}>
      <StrategyEditor
        strategy={strategy}
        versions={[strategy]}
        onSaved={() => {}}
      />
    </QueryClientProvider>
  );
  const view = render(content(initial));
  await user.clear(screen.getByLabelText("전략 이름"));
  await user.type(screen.getByLabelText("전략 이름"), "내 초안");
  view.rerender(content(latest));
  expect(screen.getByLabelText("전략 이름")).toHaveValue("내 초안");
  expect(
    screen.getByRole("button", { name: "새 버전으로 저장" }),
  ).toBeDisabled();
  await user.click(
    screen.getByRole("button", { name: "내 변경을 최신값에 재적용" }),
  );
  expect(screen.getByLabelText("전략 이름")).toHaveValue("내 초안");
  expect(screen.getByLabelText("검토 기준 점수")).toHaveValue(75);
  expect(
    screen.getByRole("button", { name: "새 버전으로 저장" }),
  ).toBeEnabled();
});
