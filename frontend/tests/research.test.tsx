import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ResearchResult } from "../src/features/researchResult";

describe("research evidence", () => {
  it("separates prediction error, constant baseline and portfolio outcomes", () => {
    render(
      <ResearchResult
        value={{
          result: {
            training: [
              {
                model_type: "ridge",
                ablation: "technical",
                holdout: { samples: 200, decision_dates: 50, mse: 0.00012 },
                constant_baseline_holdout: { mse: 0.0025 },
                portfolio_comparison: {
                  rule: {
                    metrics: {
                      return: 0.08,
                      max_drawdown: -0.03,
                      closed_trades: 8,
                    },
                  },
                  model: {
                    metrics: {
                      return: 0.04,
                      max_drawdown: -0.02,
                      closed_trades: 5,
                    },
                  },
                },
              },
            ],
          },
        }}
      />,
    );
    const tables = screen.getAllByRole("table");
    expect(within(tables[0]).getByText("0.00012000")).toBeInTheDocument();
    expect(within(tables[0]).getByText("0.0025000")).toBeInTheDocument();
    expect(within(tables[1]).getByText("규칙만 사용")).toBeInTheDocument();
    expect(within(tables[1]).getByText("8.00%")).toBeInTheDocument();
    expect(within(tables[1]).getByText("4.00%")).toBeInTheDocument();
    expect(
      screen.getByText(/포트폴리오 운용 수익이 아닙니다/),
    ).toBeInTheDocument();
  });
});
