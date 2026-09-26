import type { DataRecord } from "../api";
import { won } from "../api";
import { Details, display, RecordsTable } from "./shared";

const record = (value: unknown): DataRecord =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as DataRecord)
    : {};
const list = (value: unknown): DataRecord[] =>
  Array.isArray(value)
    ? (value.filter((v) => v && typeof v === "object") as DataRecord[])
    : [];
const ratio = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value)
    ? `${(value * 100).toFixed(2)}%`
    : "—";
const metric = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value)
    ? value.toPrecision(5)
    : "—";
export function ResearchResult({ value }: { value: DataRecord }) {
  const result = record(value.result);
  const backtests = list(result.backtests);
  const training = list(result.training);
  return (
    <>
      {value.state && (
        <p className="form-note">
          작업 상태: {display(value.state)}
          {value.error ? ` · ${display(value.error)}` : ""}
        </p>
      )}
      {result.error && (
        <div className="error-box" role="alert">
          {display(result.error)}
        </div>
      )}
      {backtests.length > 0 && (
        <>
          <h3>전략별 비용 후 성과</h3>
          <RecordsTable
            rows={backtests.map((r) => ({
              ...record(r.metrics),
              strategy: record(r.strategy).strategy_type,
            }))}
            columns={[
              { key: "strategy", title: "전략" },
              { key: "return", title: "총수익률", render: ratio },
              {
                key: "benchmark_return",
                title: "비교 기준 수익률",
                render: ratio,
              },
              { key: "max_drawdown", title: "최대 낙폭", render: ratio },
              { key: "closed_trades", title: "청산 거래" },
              {
                key: "fees_and_taxes",
                title: "비용·세금",
                render: (v) => `${won(String(v))}원`,
              },
            ]}
          />
          {backtests.map((run, i) => (
            <section key={i}>
              <h3>{display(record(run.strategy).strategy_type)} 자산 경로</h3>
              <EquityCurve rows={list(run.equity_curve)} />
              <details className="research-details">
                <summary>체결·제외 사유·실험 가정</summary>
                <Details
                  record={{
                    trades: run.trades,
                    unfilled_or_excluded: run.unfilled_or_excluded,
                    assumptions: run.assumptions,
                  }}
                />
              </details>
            </section>
          ))}
        </>
      )}
      {training.length > 0 && (
        <>
          <h3>모델의 미사용 구간 평가</h3>
          <p className="form-note">
            아래 목표 변수 수익과 순위 지표는 포트폴리오 운용 수익이 아닙니다.
            같은 레이블·날짜 분할에서 고정 기준 예측과 비교합니다.
          </p>
          <RecordsTable
            rows={training.map((r) => ({
              ...record(r.holdout),
              model_type: r.model_type,
              ablation: r.ablation,
              baseline_mse: record(r.constant_baseline_holdout).mse,
            }))}
            columns={[
              { key: "model_type", title: "모델" },
              { key: "ablation", title: "입력 조합" },
              { key: "samples", title: "표본 수" },
              { key: "decision_dates", title: "판단 날짜 수" },
              { key: "rank_ic", title: "순위 IC" },
              { key: "mse", title: "예측 MSE", render: metric },
              { key: "baseline_mse", title: "기준 MSE", render: metric },
              { key: "brier", title: "Brier" },
              { key: "roc_auc", title: "ROC AUC" },
            ]}
          />
          {training.map((r, i) => (
            <section key={i}>
              {Boolean(r.portfolio_comparison) && (
                <>
                  <h3>
                    {display(r.model_type)} · 같은 조건의 모의 포트폴리오 비교
                  </h3>
                  <RecordsTable
                    rows={["rule", "model"].map((key) => ({
                      ...record(
                        record(record(r.portfolio_comparison)[key]).metrics,
                      ),
                      strategy: key === "rule" ? "규칙만 사용" : "규칙 + 모델",
                    }))}
                    columns={[
                      { key: "strategy", title: "진입 기준" },
                      { key: "return", title: "비용 후 수익률", render: ratio },
                      {
                        key: "max_drawdown",
                        title: "최대 낙폭",
                        render: ratio,
                      },
                      { key: "closed_trades", title: "청산 거래" },
                    ]}
                  />
                  <p className="form-note">
                    동일한 미사용 기간·거래 비용·위험 규칙을 적용한 재현
                    실험입니다. 실제 한 달 운용 기록과 구분합니다.
                  </p>
                </>
              )}
              <details className="research-details">
                <summary>
                  {display(r.model_type)} / {display(r.ablation)} · 보정과 재현
                  근거
                </summary>
                <Details
                  record={{
                    holdout: r.holdout,
                    constant_baseline_holdout: r.constant_baseline_holdout,
                    artifact: r.artifact,
                    validation_trials: r.validation_trials,
                  }}
                />
                {list(record(r.holdout).calibration_bins).length > 0 && (
                  <RecordsTable
                    rows={list(record(r.holdout).calibration_bins)}
                    columns={[
                      { key: "count", title: "표본 수" },
                      {
                        key: "predicted",
                        title: "평균 예측 확률",
                        render: ratio,
                      },
                      { key: "observed", title: "관측 비율", render: ratio },
                    ]}
                  />
                )}
              </details>
            </section>
          ))}
        </>
      )}
      {value.technical && (
        <section>
          <h3>기술 피처</h3>
          <Details record={record(value.technical)} />
        </section>
      )}
      {value.financial && (
        <section>
          <h3>재무 피처·공개 시점</h3>
          <Details record={record(value.financial)} />
        </section>
      )}
      {value.macro && (
        <section>
          <h3>매크로 관측·수정본</h3>
          <Details record={record(value.macro)} />
        </section>
      )}
      {value.metrics && (
        <section>
          <h3>모델 평가 지표</h3>
          <Details record={record(value.metrics)} />
        </section>
      )}
      <details className="research-details">
        <summary>전체 원천·버전·설정 명세</summary>
        <Details record={value} />
      </details>
    </>
  );
}
export function EquityCurve({ rows }: { rows: DataRecord[] }) {
  const points = rows
    .map((r) => ({ session: display(r.session), value: Number(r.equity) }))
    .filter((r) => Number.isFinite(r.value));
  if (points.length < 2)
    return <p className="form-note">자산 경로를 그릴 관측이 부족합니다.</p>;
  const min = Math.min(...points.map((p) => p.value)),
    max = Math.max(...points.map((p) => p.value));
  const span = max - min || 1;
  const path = points
    .map(
      (p, i) =>
        `${i ? "L" : "M"}${40 + (i / (points.length - 1)) * 630},${180 - ((p.value - min) / span) * 145}`,
    )
    .join(" ");
  return (
    <div className="equity-curve">
      <svg
        viewBox="0 0 750 235"
        role="img"
        aria-label={`평가 자산 경로, ${points[0].session}부터 ${points.at(-1)!.session}까지`}
      >
        <line x1="40" x2="675" y1="180" y2="180" stroke="#b7c0d2" />
        <path d={path} stroke="#4659c2" strokeWidth="2.5" fill="none" />
        <text x="680" y="40" fontSize="12" fill="#344057">
          {won(max)}
        </text>
        <text x="680" y="180" fontSize="12" fill="#344057">
          {won(min)}
        </text>
        <text x="40" y="215" fontSize="12" fill="#344057">
          {points[0].session}
        </text>
        <text x="670" y="215" textAnchor="end" fontSize="12" fill="#344057">
          {points.at(-1)!.session}
        </text>
      </svg>
      <details className="research-details">
        <summary>자산 경로 데이터 표</summary>
        <RecordsTable
          rows={rows}
          columns={[
            { key: "session", title: "거래일" },
            {
              key: "equity",
              title: "평가 자산",
              render: (v) => won(String(v)),
            },
            { key: "drawdown", title: "낙폭", render: ratio },
            { key: "exposure", title: "시장 노출", render: ratio },
          ]}
        />
      </details>
    </div>
  );
}
