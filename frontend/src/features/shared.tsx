import { createContext, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { api, ApiError } from "../api";
import type { Context, DataRecord, Readiness } from "../api";

const WorkspaceContext = createContext<{
  data?: Context;
  error: Error | null;
  loading: boolean;
}>({ error: null, loading: true });
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const query = useQuery({
    queryKey: ["context"],
    queryFn: api.context,
    refetchInterval: 15_000,
    retry: 1,
  });
  const scope = query.data
    ? `${query.data.mode}:${query.data.account_id}`
    : "unavailable";
  const client = useMemo(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 15_000,
            retry: (count, error) =>
              !(
                error instanceof ApiError &&
                error.status >= 400 &&
                error.status < 500
              ) && count < 1,
            refetchOnWindowFocus: true,
          },
          mutations: { retry: false },
        },
      }),
    [scope],
  );
  return (
    <WorkspaceContext.Provider
      value={{ data: query.data, error: query.error, loading: query.isPending }}
    >
      <QueryClientProvider key={scope} client={client}>
        {children}
      </QueryClientProvider>
    </WorkspaceContext.Provider>
  );
}
export const useWorkspace = () => useContext(WorkspaceContext);

export function PageHeader({
  title,
  eyebrow,
  children,
}: {
  title: string;
  eyebrow: string;
  children?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        {children && <p>{children}</p>}
      </div>
    </div>
  );
}
export function ErrorNotice({ error }: { error: Error | null | undefined }) {
  if (!error) return null;
  return (
    <div className="error-box" role="alert">
      {error.message}
      {error instanceof ApiError && error.correlationId && (
        <small>문의 식별자 {error.correlationId}</small>
      )}
    </div>
  );
}
export function Loading({
  children = "자료를 확인하고 있습니다…",
}: {
  children?: ReactNode;
}) {
  return (
    <div className="empty" role="status" aria-busy="true">
      {children}
    </div>
  );
}
export function RefreshNote({
  fetching,
  error,
  updatedAt,
}: {
  fetching: boolean;
  error?: Error | null;
  updatedAt?: number;
}) {
  return (
    <div className="freshness-note" role="status">
      {fetching
        ? "최신 자료 확인 중"
        : error
          ? "갱신 실패 · 이전 자료를 표시합니다"
          : updatedAt
            ? `마지막 조회 ${new Date(updatedAt).toLocaleTimeString("ko-KR")}`
            : "아직 조회하지 않음"}
      {error && ` · ${error.message}`}
    </div>
  );
}
export function ReadinessList({ items = [] }: { items?: Readiness[] }) {
  if (!items.length)
    return <p className="form-note">준비 상태 자료가 아직 없습니다.</p>;
  return (
    <ul className="readiness-list">
      {items.map((item) => (
        <li key={item.name}>
          <span className={`status-pill ${item.ready ? "ready" : "waiting"}`}>
            {item.ready ? "준비됨" : "확인 필요"}
          </span>
          <div>
            <strong>{item.name}</strong>
            <p>{item.message}</p>
          </div>
        </li>
      ))}
    </ul>
  );
}
export const display = (value: unknown): string => {
  if (value == null || value === "") return "—";
  if (typeof value === "number")
    return Number.isFinite(value)
      ? value.toLocaleString("ko-KR", { maximumFractionDigits: 4 })
      : "—";
  if (typeof value === "boolean") return value ? "예" : "아니요";
  if (typeof value === "object")
    return Array.isArray(value) ? `${value.length}개` : "상세 자료";
  return String(value);
};
export const timestamp = (value: unknown) =>
  typeof value === "string" && !Number.isNaN(Date.parse(value))
    ? new Date(value).toLocaleString("ko-KR")
    : display(value);
export type Column = {
  key: string;
  title: string;
  render?: (value: unknown, row: DataRecord) => ReactNode;
};
export function RecordsTable({
  rows,
  columns,
  empty = "아직 수집된 자료가 없습니다.",
  onSelect,
}: {
  rows: DataRecord[];
  columns: Column[];
  empty?: string;
  onSelect?: (row: DataRecord) => void;
}) {
  const [page, setPage] = useState(0);
  const pageSize = 50;
  const last = Math.max(0, Math.ceil(rows.length / pageSize) - 1);
  const current = Math.min(page, last);
  if (!rows.length) return <div className="empty">{empty}</div>;
  return (
    <>
      <div
        className="table-scroll"
        tabIndex={0}
        role="region"
        aria-label="자료 표"
      >
        <table>
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key} scope="col">
                  {c.title}
                </th>
              ))}
              {onSelect && <th>상세</th>}
            </tr>
          </thead>
          <tbody>
            {rows
              .slice(current * pageSize, (current + 1) * pageSize)
              .map((row, i) => (
                <tr key={String(row.id ?? row.symbol ?? i)}>
                  {columns.map((c) => (
                    <td key={c.key}>
                      {c.render
                        ? c.render(row[c.key], row)
                        : display(row[c.key])}
                    </td>
                  ))}
                  {onSelect && (
                    <td>
                      <button
                        className="text-button"
                        onClick={() => onSelect(row)}
                      >
                        상세 보기
                      </button>
                    </td>
                  )}
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      {rows.length > pageSize && (
        <div className="page-controls">
          <button
            className="button secondary"
            disabled={current === 0}
            onClick={() => setPage(current - 1)}
          >
            이전 자료
          </button>
          <span className="form-note">
            {current * pageSize + 1}–
            {Math.min((current + 1) * pageSize, rows.length)} / 전체{" "}
            {rows.length}개
          </span>
          <button
            className="button secondary"
            disabled={current === last}
            onClick={() => setPage(current + 1)}
          >
            다음 자료
          </button>
        </div>
      )}
    </>
  );
}
export function Details({ record }: { record: DataRecord }) {
  return (
    <dl className="record-details">
      {Object.entries(record)
        .filter(([, value]) => value != null)
        .map(([key, value]) => (
          <div key={key}>
            <dt>{fieldLabels[key] ?? key}</dt>
            <dd>
              {value && typeof value === "object" ? (
                <pre>{JSON.stringify(value, null, 2)}</pre>
              ) : (
                display(value)
              )}
            </dd>
          </div>
        ))}
    </dl>
  );
}
const fieldLabels: Record<string, string> = {
  id: "식별자",
  name: "이름",
  kind: "종류",
  status: "상태",
  state: "상태",
  created_at: "생성 시각",
  updated_at: "갱신 시각",
  available_at: "이용 가능 시각",
  observed_at: "관측 시각",
  source: "출처",
  source_id: "원천 식별자",
  revision: "수정본",
  dataset_id: "데이터셋",
  dataset_hash: "데이터 해시",
  code_version: "코드 버전",
  strategy_version: "전략 버전",
  model_version: "모델 버전",
  model_type: "모델 유형",
  metrics: "평가 지표",
  parameters: "실험 설정",
  manifest: "재현 명세",
  error: "오류",
  train_start: "학습 시작",
  train_end: "학습 종료",
  test_start: "평가 시작",
  test_end: "평가 종료",
  row_count: "관측 수",
  missing_reason: "자료 없음 사유",
  symbol: "종목 코드",
  value: "값",
  score: "연구 점수",
  probability: "확률 추정",
  benchmark: "비교 기준",
};
