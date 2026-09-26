import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api";
import type { Deployment } from "../api";
import { ErrorNotice, Loading } from "./shared";

const ruleFields: [string, string, number, number, string][] = [
  ["min_turnover", "최소 거래대금 (원)", 0, 1e15, "1"],
  ["rsi_low", "RSI 하한", 0, 100, "0.1"],
  ["rsi_high", "RSI 상한", 0, 100, "0.1"],
  ["stop_atr", "손절 거리 (ATR 배수)", 0.01, 100, "0.01"],
  ["max_holding_sessions", "최대 보유 거래일", 1, 1000, "1"],
  ["max_positions", "최대 보유 종목 수", 1, 100, "1"],
  ["max_position_pct", "종목당 한도 (%)", 0.01, 100, "0.01"],
  ["max_total_pct", "전체 투자 한도 (%)", 0.01, 100, "0.01"],
  ["risk_per_trade_pct", "주문당 손실 예산 (%)", 0.001, 100, "0.001"],
  ["max_sector_pct", "업종당 한도 (%)", 0.01, 100, "0.01"],
  ["max_index_pct", "추종지수당 한도 (%)", 0.01, 100, "0.01"],
  ["daily_loss_limit_pct", "일 손실 중지 기준 (%)", 0.01, 100, "0.01"],
  ["max_gap_pct", "진입 가격 차이 한도 (%)", 0, 100, "0.01"],
  ["max_vix", "VIX 허용 상한", 0, 200, "0.1"],
];
export function DeploymentPanel() {
  const query = useQuery({ queryKey: ["deployment"], queryFn: api.deployment });
  return (
    <section className="panel padded-panel">
      <h2>자동 모의운용 설정</h2>
      <p className="form-note">
        아래 설정은 실제 운용에 사용하는 고정 버전입니다. 기존 가중 점수 편집은
        탐색용입니다. 운용 구성은 하나를 선택하고 다른 모델은 주문 없는 비교로
        유지합니다.
      </p>
      {!query.data ? (
        query.isPending ? (
          <Loading />
        ) : (
          <ErrorNotice error={query.error} />
        )
      ) : (
        <DeploymentEditor value={query.data} />
      )}
    </section>
  );
}
function DeploymentEditor({ value }: { value: Deployment }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState(value);
  const [base, setBase] = useState(value);
  const dirty = JSON.stringify(base) !== JSON.stringify(draft);
  useEffect(() => {
    if (!dirty && value.version !== base.version) {
      setBase(value);
      setDraft(value);
    }
  }, [value, base.version, dirty]);
  const models = useQuery({ queryKey: ["research"], queryFn: api.research });
  const save = useMutation({
    mutationFn: api.saveDeployment,
    onSuccess: (saved) => {
      setBase(saved);
      setDraft(saved);
      void client.invalidateQueries();
    },
  });
  const conflict =
    value.version !== draft.version ||
    (save.error instanceof ApiError && save.error.status === 409);
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (!conflict) save.mutate(draft);
      }}
    >
      <p className="form-note">
        편집 기준 v{draft.version} · 서버 최신 v{value.version} · 적용 시각{" "}
        {new Date(value.updated_at).toLocaleString("ko-KR")}
      </p>
      <fieldset disabled={save.isPending}>
        <div className="settings-grid">
          <label className="field">
            운용 전략
            <select
              value={draft.strategy_type}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  strategy_type: e.target.value as Deployment["strategy_type"],
                })
              }
            >
              <option value="kospi_pullback">코스피 상승 추세 눌림목</option>
              <option value="etf_trend">ETF 추세</option>
            </select>
          </label>
          <label className="field">
            사용 모델
            <select
              value={draft.model_id ?? ""}
              onChange={(e) =>
                setDraft({ ...draft, model_id: e.target.value || null })
              }
            >
              <option value="">고정 규칙만 사용</option>
              {(models.data?.models ?? [])
                .filter((model) => model.status === "selected")
                .map((model) => (
                  <option key={String(model.id)} value={String(model.id)}>
                    {String(model.name ?? model.model_type ?? model.id)}
                  </option>
                ))}
            </select>
          </label>
          {ruleFields
            .filter(([key]) => typeof draft.rules[key] === "number")
            .map(([key, label, min, max, step]) => (
              <label className="field" key={key}>
                {label}
                <input
                  type="number"
                  min={min}
                  max={max}
                  step={step}
                  required
                  value={Number(draft.rules[key])}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      rules: { ...draft.rules, [key]: Number(e.target.value) },
                    })
                  }
                />
              </label>
            ))}
        </div>
        <div className="page-controls">
          {[
            ["use_quality", "재무 가치·품질 필터 사용"],
            ["use_macro", "매크로 위험 조절 사용"],
          ].map(([key, label]) => (
            <label className="checkbox-field" key={key}>
              <input
                type="checkbox"
                checked={Boolean(draft.rules[key])}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    rules: { ...draft.rules, [key]: e.target.checked },
                  })
                }
              />
              {label}
            </label>
          ))}
        </div>
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
          />
          자동 모의운용 활성화
        </label>
        <p className="form-note">
          활성화는 모의 운용만 대상으로 합니다. 별도 작업자가 실행 중이어야
          하며, 데이터·주문 대조가 준비되지 않으면 서버가 진입을 차단합니다.
        </p>
        {conflict && (
          <div className="warning-note" role="alert">
            운용 설정이 다른 곳에서 변경되었습니다. 현재 초안은 유지했습니다.
            <div className="page-controls">
              <button
                type="button"
                className="button secondary"
                onClick={() =>
                  void client.invalidateQueries({ queryKey: ["deployment"] })
                }
              >
                최신 운용 설정 조회
              </button>
              <button
                type="button"
                className="button secondary"
                onClick={() => {
                  setBase(value);
                  setDraft(value);
                  save.reset();
                }}
              >
                최신 설정으로 교체
              </button>
              <button
                type="button"
                className="button secondary"
                disabled={value.version === draft.version}
                onClick={() => {
                  setBase(value);
                  setDraft({ ...draft, version: value.version });
                  save.reset();
                }}
              >
                내 초안으로 최신 버전 다시 편집
              </button>
            </div>
          </div>
        )}
        <button className="button primary" disabled={conflict}>
          운용 설정 새 버전 저장
        </button>
      </fieldset>
      <ErrorNotice error={save.error} />
      {save.isSuccess && (
        <p className="success" role="status">
          운용 설정 v{save.data.version}을 저장했습니다.
        </p>
      )}
    </form>
  );
}
