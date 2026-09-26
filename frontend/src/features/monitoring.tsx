import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, won, pct } from "../api";
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
  timestamp,
  useWorkspace,
} from "./shared";

export function WatchlistPage() {
  const query = useQuery({
    queryKey: ["watchlist"],
    queryFn: api.watchlist,
    refetchInterval: 5_000,
  });
  const [selected, setSelected] = useState<DataRecord | null>(null);
  return (
    <>
      <PageHeader title="후보·보유 감시" eyebrow="WATCHLIST">
        확정 일봉의 후보와 보유 종목을 우선 확인합니다. 화면을 닫아도 서버
        작업은 별도로 동작합니다.
      </PageHeader>
      <section className="panel padded-panel">
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
            <p className="form-note">
              분석 기준 {query.data.as_of ?? "미확인"} · 원천{" "}
              {query.data.source}
            </p>
            <RecordsTable
              rows={query.data.items}
              columns={[
                { key: "symbol", title: "종목" },
                { key: "name", title: "이름" },
                { key: "rank", title: "연구 순위 값" },
                { key: "eligible", title: "조건 충족" },
                { key: "strategy_version", title: "전략 버전" },
                { key: "as_of", title: "분석 기준" },
              ]}
              empty="아직 감시 후보가 없습니다. 확정 일봉 수집과 분석 작업을 확인하세요."
              onSelect={setSelected}
            />
            {selected && <Details record={selected} />}
          </>
        )}
      </section>
    </>
  );
}

export function OperationsPage() {
  const client = useQueryClient();
  const workspace = useWorkspace();
  const [kind, setKind] = useState("reconcile");
  const [payload, setPayload] = useState("{}");
  const [error, setError] = useState<Error | null>(null);
  const [selected, setSelected] = useState<DataRecord | null>(null);
  const query = useQuery({
    queryKey: ["operations"],
    queryFn: api.operations,
    refetchInterval: 15_000,
  });
  const run = useMutation({
    mutationFn: ({
      kind: job,
      payload: data,
    }: {
      kind: string;
      payload: DataRecord;
    }) => api.runJob(job, data),
    onSuccess: (result) => {
      setSelected(result);
      void client.invalidateQueries();
    },
  });
  const control = useMutation({
    mutationFn: api.control,
    onSuccess: () => {
      void client.invalidateQueries();
    },
  });
  return (
    <>
      <PageHeader title="운영 상태" eyebrow="OPERATIONS">
        수집·분석·주문 대조와 작업 복구를 확인합니다. 신규 진입 중지는 기존 주문
        취소나 전량 청산을 뜻하지 않습니다.
      </PageHeader>
      <section className="panel padded-panel">
        <h2>연결·준비 상태</h2>
        <ReadinessList items={workspace.data?.readiness} />
        <ErrorNotice error={workspace.error} />
      </section>
      <section className="panel padded-panel">
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
            <div className="page-controls">
              <strong>
                신규 진입: {query.data.entries_enabled ? "허용" : "중지"}
              </strong>
              <button
                className={
                  query.data.entries_enabled
                    ? "button secondary"
                    : "button primary"
                }
                disabled={control.isPending}
                onClick={() => control.mutate(!query.data!.entries_enabled)}
              >
                {query.data.entries_enabled
                  ? "신규 진입 중지"
                  : "서버 점검 후 신규 진입 재개"}
              </button>
            </div>
            <ErrorNotice error={control.error} />
            <h2>작업 실행</h2>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                setError(null);
                try {
                  const data: unknown = JSON.parse(payload);
                  if (!data || typeof data !== "object" || Array.isArray(data))
                    throw new Error("작업 조건은 JSON 객체여야 합니다.");
                  run.mutate({ kind, payload: data as DataRecord });
                } catch (e) {
                  setError(
                    e instanceof Error
                      ? e
                      : new Error("작업 조건을 확인하세요."),
                  );
                }
              }}
            >
              <fieldset disabled={run.isPending}>
                <div className="page-controls">
                  <label className="field">
                    작업 종류
                    <select
                      aria-label="작업 종류"
                      value={kind}
                      onChange={(e) => setKind(e.target.value)}
                    >
                      <option value="reconcile">주문·잔고 대조</option>
                      <option value="collect_market">시세·일봉 수집</option>
                      <option value="analyze">확정 일봉 분석</option>
                      <option value="daily_pipeline">일별 작업 실행</option>
                      <option value="collect_source">외부 원천 수집</option>
                      <option value="build_dataset">
                        수집 자료로 연구 데이터셋 생성
                      </option>
                      <option value="auto_trade">
                        자동 모의운용 판단 실행
                      </option>
                      <option value="report">일별 보고 기록</option>
                      <option value="backup">백업</option>
                    </select>
                  </label>
                  <button className="button primary">작업 요청</button>
                </div>
                {kind === "auto_trade" && (
                  <p className="warning-note" role="status">
                    {workspace.data?.mode === "kis_paper"
                      ? "한투 모의계좌"
                      : "로컬 시뮬레이션"}
                    의 현재 운용 전략으로 판단과 주문을 요청합니다. 서버의 자동
                    모의운용·신규 진입 설정과 위험 한도를 적용합니다.
                  </p>
                )}
                {kind === "collect_source" && (
                  <p className="form-note">
                    추가 조건에 provider(dart·fred·krx), operation, parameters를
                    넣습니다. 키는 서버 환경에서만 읽으며 이 입력에 넣지
                    않습니다. 수집 결과를 기존 데이터셋에 합치려면
                    base_dataset_id와 symbol_map을 지정하세요.
                  </p>
                )}
                {kind === "build_dataset" && (
                  <p className="form-note">
                    추가 조건의 start·end 거래일과 name으로 수집 자료의 범위를
                    정합니다. 실제 원천·공개 시각·비교 기준이 부족하면 범위
                    제한으로 표시됩니다.
                  </p>
                )}
                <details className="research-details">
                  <summary>작업별 추가 조건</summary>
                  {["collect_source", "build_dataset"].includes(kind) && (
                    <button
                      type="button"
                      className="button secondary"
                      onClick={() =>
                        setPayload(
                          kind === "collect_source"
                            ? '{"provider":"fred","operation":"observations","parameters":{"series_id":"VIXCLS"}}'
                            : '{"start":"2024-01-02","end":"2024-12-30","name":"연구 자료"}',
                        )
                      }
                    >
                      조건 예시 넣기
                    </button>
                  )}
                  <label className="field">
                    조건 JSON
                    <textarea
                      placeholder={
                        kind === "collect_source"
                          ? '{"provider":"fred","operation":"observations","parameters":{"series_id":"VIXCLS"}}'
                          : kind === "build_dataset"
                            ? '{"start":"2024-01-02","end":"2024-12-30","name":"연구 자료"}'
                            : "{}"
                      }
                      rows={3}
                      value={payload}
                      onChange={(e) => setPayload(e.target.value)}
                    />
                  </label>
                  {kind === "collect_source" && (
                    <p className="form-note">
                      DART: companies·filings·financial_facts, FRED:
                      observations·vintages, KRX: daily. 원천별 필수 조건과 조회
                      권한은 서버 검증 결과를 확인하세요.
                    </p>
                  )}
                </details>
              </fieldset>
            </form>
            <ErrorNotice error={error ?? run.error} />
            <h2>최근 작업</h2>
            <RecordsTable
              rows={query.data.jobs}
              columns={[
                { key: "kind", title: "작업" },
                { key: "state", title: "상태" },
                { key: "started_at", title: "시작" },
                { key: "finished_at", title: "종료" },
                { key: "error", title: "오류" },
              ]}
              onSelect={setSelected}
            />
            <h2>주문·잔고 대조</h2>
            <RecordsTable
              rows={[...query.data.reconciliations].reverse()}
              columns={[
                { key: "at", title: "시각", render: timestamp },
                {
                  key: "status",
                  title: "결과",
                  render: (v) =>
                    v === "matched"
                      ? "일치"
                      : v === "mismatch"
                        ? "차이 발견"
                        : display(v),
                },
                {
                  key: "issues",
                  title: "설명",
                  render: (v) =>
                    Array.isArray(v)
                      ? v.map(String).join(" · ") || "—"
                      : display(v),
                },
              ]}
              onSelect={setSelected}
            />
            {selected && (
              <section className="research-result">
                <h2>작업 상세</h2>
                <Details
                  record={
                    query.data.jobs.find((job) => job.id === selected.id) ??
                    selected
                  }
                />
              </section>
            )}
            <details className="research-details">
              <summary>서비스 상태 상세</summary>
              {Array.isArray(query.data.health) ? (
                query.data.health.map((h, i) => <Details key={i} record={h} />)
              ) : (
                <Details record={query.data.health} />
              )}
            </details>
          </>
        )}
      </section>
    </>
  );
}

export function ReportsPage() {
  const query = useQuery({
    queryKey: ["reports"],
    queryFn: api.reports,
    refetchInterval: 30_000,
  });
  const [selected, setSelected] = useState<DataRecord | null>(null);
  return (
    <>
      <PageHeader title="성과·운용 보고" eyebrow="PERFORMANCE & RELIABILITY">
        비용 후 성과와 실행 오류를 분리합니다. 한 달 기록만으로 전략의 수익성을
        확정하지 않습니다.
      </PageHeader>
      <section className="panel padded-panel">
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
            <p
              className={
                query.data.observation_complete ? "form-note" : "warning-note"
              }
              role="status"
            >
              {query.data.observation_complete
                ? "관찰 기간의 완료 조건을 확인했습니다."
                : "한 달 관찰은 아직 완료되지 않았습니다."}
              {typeof query.data.note === "string" && ` ${query.data.note}`}
            </p>
            {Array.isArray(query.data.observation_reasons) &&
              query.data.observation_reasons.length > 0 && (
                <ul className="form-note">
                  {query.data.observation_reasons.map((reason) => (
                    <li key={String(reason)}>
                      {observationReasons[String(reason)] ?? String(reason)}
                    </li>
                  ))}
                </ul>
              )}
            <h2>일별 자산·운영 기록</h2>
            <RecordsTable
              rows={query.data.daily}
              columns={[
                { key: "session", title: "거래일" },
                {
                  key: "equity",
                  title: "평가 자산",
                  render: (value) =>
                    value == null ? "미조회" : `${won(String(value))}원`,
                },
                {
                  key: "realized_profit",
                  title: "실현 손익",
                  render: (value) =>
                    value == null ? "—" : `${won(String(value))}원`,
                },
                { key: "fees", title: "비용 합계", render: reportMoney },
                { key: "unresolved_count", title: "미확정 주문" },
                { key: "reconciliation", title: "대조 상태" },
                { key: "no_trade_reason", title: "무거래 사유" },
              ]}
              empty="실제로 기록된 일별 스냅샷이 없습니다. 운영 작업에서 대조와 일별 보고를 완료하세요."
              onSelect={setSelected}
            />
            <h2>월간 관찰</h2>
            <RecordsTable
              rows={query.data.monthly}
              columns={[
                { key: "month", title: "기간" },
                { key: "observed_sessions", title: "관찰 거래일" },
                {
                  key: "start_equity",
                  title: "시작 자산",
                  render: (v) => won(String(v)),
                },
                {
                  key: "end_equity",
                  title: "종료 자산",
                  render: (v) => won(String(v)),
                },
                { key: "max_drawdown_pct", title: "최대 낙폭 (%)" },
                {
                  key: "return_pct",
                  title: "구간 수익률",
                  render: (v) => (typeof v === "number" ? pct(v) : "미확인"),
                },
                { key: "fees", title: "비용 합계", render: reportMoney },
                { key: "verified", title: "대조 확인" },
              ]}
              empty="아직 월간 관찰 보고가 없습니다. 실제 관찰 기간과 재현 실험은 구분됩니다."
              onSelect={setSelected}
            />
            {query.data.baseline && (
              <details className="research-details">
                <summary>비교 기준과 자료 제한</summary>
                {Array.isArray(query.data.baseline) ? (
                  query.data.baseline.map((r, i) => (
                    <Details key={i} record={r} />
                  ))
                ) : (
                  <Details record={query.data.baseline} />
                )}
              </details>
            )}
            {selected && <Details record={selected} />}
          </>
        )}
      </section>
    </>
  );
}
const reportMoney = (value: unknown) => {
  const amount = value == null ? "—" : won(String(value));
  return amount === "—" ? "미확인" : `${amount}원`;
};
const observationReasons: Record<string, string> = {
  synthetic_mode: "로컬 예제 자료로는 실제 모의계좌 관찰을 완료할 수 없습니다.",
  observation_period_not_set: "관찰 시작일·종료일이 설정되지 않았습니다.",
  observation_period_not_elapsed: "설정한 관찰 기간이 아직 지나지 않았습니다.",
  calendar_coverage_missing: "관찰 기간의 거래 달력이 부족합니다.",
  insufficient_trading_sessions: "필요한 관찰 거래일 수가 부족합니다.",
  missing_or_unverified_daily_reports:
    "일 마감 보고나 주문·잔고 대조가 완료되지 않은 거래일이 있습니다.",
  unresolved_orders: "결과가 미확정인 주문이 남아 있습니다.",
};
