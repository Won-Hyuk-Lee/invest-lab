import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { DataRecord } from "../api";
import {
  Details,
  display,
  ErrorNotice,
  Loading,
  PageHeader,
  ReadinessList,
  RecordsTable,
  RefreshNote,
} from "./shared";
import { ResearchResult } from "./researchResult";

type Tab =
  "datasets" | "features" | "fundamentals" | "macro" | "experiments" | "models";
const tabs: [Tab, string][] = [
  ["datasets", "데이터셋"],
  ["features", "피처"],
  ["fundamentals", "재무·공시"],
  ["macro", "매크로"],
  ["experiments", "백테스트·비교"],
  ["models", "통계 모델"],
];
export function ResearchPage() {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["research"],
    queryFn: api.research,
    refetchInterval: 15_000,
  });
  const [tab, setTab] = useState<Tab>("datasets");
  const [datasetId, setDatasetId] = useState("");
  const [asOf, setAsOf] = useState("");
  const [symbol, setSymbol] = useState("");
  const [selected, setSelected] = useState<DataRecord | null>(null);
  const datasets = query.data?.datasets ?? [];
  const chosen = datasetId || String(datasets[0]?.id ?? "");
  const features = useQuery({
    queryKey: ["research-features", chosen, asOf, symbol],
    queryFn: ({ signal }) =>
      api.researchFeatures(
        chosen,
        asOf ? new Date(asOf).toISOString() : undefined,
        symbol || undefined,
        signal,
      ),
    enabled:
      Boolean(chosen) && ["features", "fundamentals", "macro"].includes(tab),
  });
  const jobs = useQuery({
    queryKey: ["operations"],
    queryFn: api.operations,
    enabled: Boolean(
      selected?.kind &&
      ["research_experiment", "create_demo_dataset"].includes(
        String(selected.kind),
      ),
    ),
    refetchInterval: 3_000,
  });
  const job = jobs.data?.jobs.find((item) => item.id === selected?.id);
  useEffect(() => {
    if (job?.state === "completed" || job?.state === "succeeded")
      void client.invalidateQueries({ queryKey: ["research"] });
  }, [client, job?.id, job?.state]);
  const selectedValue =
    job?.result && typeof job.result === "object"
      ? (job.result as DataRecord)
      : (job ?? selected);
  const experimentId =
    selectedValue?.kind &&
    selectedValue?.id &&
    ["backtest", "train", "compare"].includes(String(selectedValue.kind))
      ? String(selectedValue.id)
      : "";
  const detail = useQuery({
    queryKey: ["experiment", experimentId],
    queryFn: ({ signal }) => api.experimentDetail(experimentId, signal),
    enabled: Boolean(experimentId),
    refetchInterval: 10_000,
  });
  const rows = features.data?.rows ?? [];
  return (
    <>
      <PageHeader title="연구실" eyebrow="RESEARCH WORKSPACE">
        기술·재무·매크로 입력과 통계 모델을 같은 기간·대상·비용 조건에서
        비교합니다.
      </PageHeader>
      {!query.data ? (
        query.isPending ? (
          <Loading />
        ) : (
          <ErrorNotice error={query.error} />
        )
      ) : (
        <>
          <RefreshNote
            fetching={query.isFetching}
            error={query.error}
            updatedAt={query.dataUpdatedAt}
          />
          <div className="warning-note">
            {query.data.real_validation_complete
              ? "실제 원천 검증 결과를 각 데이터셋의 범위와 함께 확인하세요."
              : "실제 자료 검증은 아직 완료되지 않았습니다. 연습 데이터 결과와 실제 자료 결과를 구분합니다."}{" "}
            모델 점수는 검증된 상승 확률을 뜻하지 않습니다.
          </div>
          {query.data.readiness && query.data.readiness.length > 0 && (
            <section className="panel padded-panel">
              <ReadinessList items={query.data.readiness} />
            </section>
          )}
          <div className="research-tabs" role="tablist" aria-label="연구 항목">
            {tabs.map(([value, label]) => (
              <button
                key={value}
                role="tab"
                aria-selected={tab === value}
                aria-controls={`research-${value}`}
                tabIndex={tab === value ? 0 : -1}
                id={`tab-${value}`}
                onKeyDown={(event) => {
                  if (
                    !["ArrowLeft", "ArrowRight", "Home", "End"].includes(
                      event.key,
                    )
                  )
                    return;
                  event.preventDefault();
                  const index = tabs.findIndex(([key]) => key === value);
                  const next =
                    event.key === "Home"
                      ? 0
                      : event.key === "End"
                        ? tabs.length - 1
                        : (index +
                            (event.key === "ArrowRight" ? 1 : -1) +
                            tabs.length) %
                          tabs.length;
                  setTab(tabs[next][0]);
                  setSelected(null);
                  requestAnimationFrame(() =>
                    document.getElementById(`tab-${tabs[next][0]}`)?.focus(),
                  );
                }}
                onClick={() => {
                  setTab(value);
                  setSelected(null);
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <section
            className="panel padded-panel"
            role="tabpanel"
            id={`research-${tab}`}
            aria-labelledby={`tab-${tab}`}
          >
            {tab === "datasets" && (
              <>
                <h2>데이터셋과 출처</h2>
                <RecordsTable
                  rows={datasets}
                  columns={[
                    { key: "name", title: "이름" },
                    { key: "source_kind", title: "자료 구분" },
                    {
                      key: "coverage",
                      title: "일봉 수",
                      render: (v) =>
                        display(
                          v && typeof v === "object"
                            ? (v as DataRecord).raw_bars
                            : null,
                        ),
                    },
                    { key: "start_session", title: "시작일" },
                    { key: "end_session", title: "종료일" },
                    { key: "created_at", title: "등록 시각" },
                  ]}
                  onSelect={setSelected}
                />
                <DatasetImport
                  onImported={(record) => {
                    setSelected(record);
                    if (record.id && record.source_kind)
                      setDatasetId(String(record.id));
                  }}
                />
              </>
            )}
            {["features", "fundamentals", "macro"].includes(tab) && (
              <>
                <div className="page-controls">
                  <label className="field">
                    데이터셋
                    <select
                      value={chosen}
                      onChange={(e) => {
                        setDatasetId(e.target.value);
                        setSelected(null);
                      }}
                    >
                      <option value="">자료를 선택하세요</option>
                      {datasets.map((d) => (
                        <option key={String(d.id)} value={String(d.id)}>
                          {display(d.name ?? d.id)} · {display(d.source_kind)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    판단 기준 시각
                    <input
                      type="datetime-local"
                      value={asOf}
                      onChange={(e) => setAsOf(e.target.value)}
                    />
                  </label>
                  <label className="field">
                    종목 코드
                    <input
                      value={symbol}
                      onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                      placeholder="전체 또는 0048K0"
                    />
                  </label>
                </div>
                <p className="form-note">
                  기준 시각까지 이용할 수 있었던 자료만 표시합니다.
                  발표일·수정본·결측 사유는 상세에서 확인하세요. 시각은 이 PC의
                  시간대 기준입니다.
                </p>
                {!chosen ? (
                  <div className="empty">먼저 데이터셋을 등록하세요.</div>
                ) : features.isPending ? (
                  <Loading />
                ) : features.error ? (
                  <ErrorNotice error={features.error} />
                ) : (
                  <>
                    <p className="form-note">
                      출처 {features.data?.source_kind ?? "—"} · 기준{" "}
                      {features.data?.as_of ?? "—"}
                    </p>
                    <RecordsTable
                      rows={rows.map((r) => ({
                        ...r,
                        selection:
                          tab === "fundamentals"
                            ? r.financial
                            : tab === "macro"
                              ? r.macro
                              : r.technical,
                      }))}
                      columns={[
                        { key: "symbol", title: "종목" },
                        { key: "name", title: "이름" },
                        {
                          key: "selection",
                          title:
                            tab === "fundamentals"
                              ? "재무 지표"
                              : tab === "macro"
                                ? "매크로 관측"
                                : "기술 피처",
                          render: (value) =>
                            value && typeof value === "object"
                              ? `${Object.keys(value).length}개 항목`
                              : "자료 없음",
                        },
                        {
                          key: "missing_reasons",
                          title: "자료 제한",
                          render: (value) =>
                            Array.isArray(value)
                              ? value.map(display).join(" · ") || "—"
                              : display(value),
                        },
                      ]}
                      onSelect={setSelected}
                    />
                    {features.data?.coverage && (
                      <details className="research-details">
                        <summary>자료 범위·품질 확인</summary>
                        <Details record={features.data.coverage} />
                      </details>
                    )}
                  </>
                )}
              </>
            )}
            {tab === "experiments" && (
              <>
                <h2>백테스트와 기준 전략 비교</h2>
                <p className="form-note">
                  같은 종목·기간·초기 자금·거래 비용·체결 정책에서 비교합니다.
                  테스트 구간으로 튜닝하지 않습니다.
                </p>
                <ExperimentForm datasets={datasets} onCreated={setSelected} />
                <RecordsTable
                  rows={query.data.experiments ?? []}
                  columns={[
                    { key: "id", title: "실험" },
                    { key: "kind", title: "유형" },
                    { key: "status", title: "상태" },
                    { key: "source_kind", title: "자료 구분" },
                    { key: "created_at", title: "생성 시각" },
                  ]}
                  onSelect={setSelected}
                />
              </>
            )}
            {tab === "models" && (
              <>
                <h2>통계 모델과 검증 이력</h2>
                <p className="form-note">
                  Ridge 수익 예측과 로지스틱 사건 확률을 구분합니다. 확률은
                  표본외 평가·보정 결과와 함께 해석하세요. 연구 모델 선택이 자동
                  주문을 활성화하지 않습니다.
                </p>
                <ExperimentForm
                  datasets={datasets}
                  training
                  onCreated={setSelected}
                />
                <RecordsTable
                  rows={query.data.models ?? []}
                  columns={[
                    { key: "id", title: "모델" },
                    { key: "model_type", title: "유형" },
                    { key: "status", title: "상태" },
                    { key: "dataset_id", title: "입력 자료" },
                    { key: "created_at", title: "학습 시각" },
                  ]}
                  onSelect={setSelected}
                />
                {selected?.id && !experimentId && (
                  <ModelStatus key={String(selected.id)} model={selected} />
                )}
              </>
            )}
            {selected && (
              <section
                className="research-result"
                aria-label="선택한 연구 자료"
              >
                <div className="panel-heading">
                  <h2>선택한 자료 · 재현 근거</h2>
                  <button
                    className="text-button"
                    onClick={() => setSelected(null)}
                  >
                    상세 닫기
                  </button>
                </div>
                {detail.isFetching && experimentId && (
                  <Loading>실험 결과 확인 중…</Loading>
                )}
                <ErrorNotice error={detail.error} />
                <ResearchResult
                  value={detail.data ?? selectedValue ?? selected}
                />
              </section>
            )}
          </section>
        </>
      )}
    </>
  );
}

function DatasetImport({
  onImported,
}: {
  onImported: (record: DataRecord) => void;
}) {
  const client = useQueryClient();
  const [format, setFormat] = useState("json");
  const [table, setTable] = useState("bars");
  const [name, setName] = useState("");
  const [base, setBase] = useState("");
  const [text, setText] = useState("");
  const [error, setError] = useState<Error | null>(null);
  const mutation = useMutation({
    mutationFn: api.importDataset,
    onSuccess: (record) => {
      onImported(record);
      void client.invalidateQueries({ queryKey: ["research"] });
    },
  });
  const submit = () => {
    setError(null);
    try {
      const payload: unknown =
        format === "csv"
          ? {
              format: "csv",
              table,
              csv_text: text,
              name: name || undefined,
              base_dataset_id: base || undefined,
            }
          : JSON.parse(text);
      if (!payload || typeof payload !== "object" || Array.isArray(payload))
        throw new Error("데이터셋은 JSON 객체여야 합니다.");
      mutation.mutate(
        name ? { ...(payload as DataRecord), name } : (payload as DataRecord),
      );
    } catch (e) {
      setError(e instanceof Error ? e : new Error("입력 형식을 확인하세요."));
    }
  };
  return (
    <details className="research-details">
      <summary>데이터 가져오기</summary>
      <p className="form-note">
        원천·공개 시각·수정본 등 재현 정보가 포함된 JSON 데이터셋 또는 정규화
        CSV를 가져옵니다. API 키가 포함된 파일을 선택하지 마세요.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <fieldset disabled={mutation.isPending}>
          <div className="page-controls">
            <label className="field">
              형식
              <select
                value={format}
                onChange={(e) => setFormat(e.target.value)}
              >
                <option value="json">데이터셋 JSON</option>
                <option value="csv">표 CSV</option>
              </select>
            </label>
            <label className="field">
              이름
              <input value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            {format === "csv" && (
              <>
                <label className="field">
                  자료 종류
                  <select
                    value={table}
                    onChange={(e) => setTable(e.target.value)}
                  >
                    {[
                      "bars",
                      "financial_facts",
                      "macro",
                      "benchmarks",
                      "etf_metadata",
                      "actions",
                      "universe",
                    ].map((t) => (
                      <option key={t}>{t}</option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  기존 데이터셋 ID (선택)
                  <input
                    value={base}
                    onChange={(e) => setBase(e.target.value)}
                  />
                </label>
              </>
            )}
          </div>
          <label className="field">
            파일 선택
            <input
              type="file"
              accept=".json,.csv,application/json,text/csv"
              onChange={async (e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                if (file.size > 20 * 1024 * 1024) {
                  setError(new Error("20MB 이하 파일을 선택하세요."));
                  return;
                }
                setText(await file.text());
              }}
            />
          </label>
          <label className="field">
            가져올 내용
            <textarea
              required
              rows={8}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={
                format === "json"
                  ? '{"name":"연구 자료","source_kind":"imported","provenance":{},"bars":[]}'
                  : "symbol,session,open,high,low,close,volume,available_at"
              }
            />
          </label>
          <button className="button primary">
            {mutation.isPending ? "검증·가져오기 중…" : "검증하고 가져오기"}
          </button>
        </fieldset>
      </form>
      <ErrorNotice error={error ?? mutation.error} />
      <button
        className="button secondary"
        disabled={mutation.isPending}
        onClick={() => {
          setError(null);
          void api
            .runJob("create_demo_dataset")
            .then((result) => {
              onImported(result);
              return client.invalidateQueries({ queryKey: ["research"] });
            })
            .catch((e: Error) => setError(e));
        }}
      >
        명시적으로 연습 데이터 생성
      </button>
      <p className="form-note">
        연습 데이터는 실제 투자 성과나 실제 원천 검증에 사용하지 않습니다.
      </p>
    </details>
  );
}

function ExperimentForm({
  datasets,
  training = false,
  onCreated,
}: {
  datasets: DataRecord[];
  training?: boolean;
  onCreated: (record: DataRecord) => void;
}) {
  const client = useQueryClient();
  const [dataset, setDataset] = useState("");
  const [kind, setKind] = useState(training ? "train" : "backtest");
  const [model, setModel] = useState("ridge");
  const [ablation, setAblation] = useState("technical");
  const [strategy, setStrategy] = useState("kospi_pullback");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [horizon, setHorizon] = useState("10");
  const [cash, setCash] = useState("10000000");
  const mutation = useMutation({
    mutationFn: api.experiment,
    onSuccess: (record) => {
      onCreated(record);
      void client.invalidateQueries({ queryKey: ["research"] });
    },
  });
  return (
    <form
      className="experiment-form"
      onSubmit={(e) => {
        e.preventDefault();
        mutation.mutate({
          kind: training ? "train" : kind,
          dataset_id: dataset,
          model_type: model,
          parameters: {
            strategy_type: strategy,
            start_session: start || undefined,
            end_session: end || undefined,
            initial_cash: Number(cash),
            horizon: Number(horizon),
            seed: 42,
            ablation,
          },
        });
      }}
    >
      <fieldset disabled={mutation.isPending}>
        <div className="settings-grid">
          <label className="field">
            데이터셋
            <select
              aria-label="데이터셋"
              required
              value={dataset}
              onChange={(e) => setDataset(e.target.value)}
            >
              <option value="">자료 선택</option>
              {datasets.map((d) => (
                <option key={String(d.id)} value={String(d.id)}>
                  {display(d.name ?? d.id)} · {display(d.source_kind)}
                </option>
              ))}
            </select>
          </label>
          {!training && (
            <label className="field">
              실험 유형
              <select value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="backtest">전략 백테스트</option>
                <option value="compare">기준 전략 비교</option>
              </select>
            </label>
          )}
          <label className="field">
            전략
            <select
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
            >
              <option value="kospi_pullback">코스피 상승 추세 눌림목</option>
              <option value="etf_trend">ETF 추세</option>
            </select>
          </label>
          <label className="field">
            모델 유형
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="ridge">Ridge 수익 예측</option>
              <option value="logistic">로지스틱 사건 확률</option>
            </select>
          </label>
          <label className="field">
            학습 입력 조합
            <select
              value={ablation}
              onChange={(e) => setAblation(e.target.value)}
            >
              <option value="technical">기술 피처</option>
              <option value="financial">기술 + 재무</option>
              <option value="macro">기술 + 매크로</option>
              <option value="all">전체 피처</option>
            </select>
          </label>
          <label className="field">
            시작 거래일 (선택)
            <input
              type="date"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </label>
          <label className="field">
            종료 거래일 (선택)
            <input
              type="date"
              min={start || undefined}
              value={end}
              onChange={(e) => setEnd(e.target.value)}
            />
          </label>
          <label className="field">
            예측 기간 (거래일)
            <select
              value={horizon}
              onChange={(e) => setHorizon(e.target.value)}
            >
              {[5, 10, 20].map((n) => (
                <option key={n}>{n}</option>
              ))}
            </select>
          </label>
          <label className="field">
            초기 자금 (원)
            <input
              type="number"
              min="1"
              step="1"
              required
              value={cash}
              onChange={(e) => setCash(e.target.value)}
            />
          </label>
        </div>
        <button className="button primary" disabled={!datasets.length}>
          {mutation.isPending
            ? "실험 요청 중…"
            : training
              ? "학습·검증 실행"
              : "실험 실행"}
        </button>
      </fieldset>
      <ErrorNotice error={mutation.error} />
      <p className="form-note">
        학습 구간·평가 구간·비용·결측 정책과 모델 보정 결과는 실행 결과의 재현
        명세에서 확인합니다.
      </p>
    </form>
  );
}

function ModelStatus({ model }: { model: DataRecord }) {
  const client = useQueryClient();
  const [status, setStatus] = useState(String(model.status ?? "candidate"));
  const mutation = useMutation({
    mutationFn: () => api.modelStatus(String(model.id), status),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["research"] });
    },
  });
  return (
    <form
      className="page-controls"
      onSubmit={(e) => {
        e.preventDefault();
        mutation.mutate();
      }}
    >
      <label className="field">
        연구 모델 상태
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="candidate">검토 후보</option>
          <option value="shadow">주문 없는 비교</option>
          <option value="selected">선택한 모델</option>
          <option value="rejected">채택하지 않음</option>
        </select>
      </label>
      <button className="button secondary" disabled={mutation.isPending}>
        상태 저장
      </button>
      <ErrorNotice error={mutation.error} />
      {mutation.isSuccess && (
        <p role="status">
          연구 상태를 저장했습니다. 자동 주문 활성화와는 별개입니다.
        </p>
      )}
    </form>
  );
}
