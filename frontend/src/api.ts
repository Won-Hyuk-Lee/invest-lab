import type { components } from "./api.generated";
type Schema = components["schemas"];

export type Kind = "kospi" | "etf_kr" | "etf_us";
export type Component = {
  key: string;
  label: string;
  value: number;
  weight: number;
  contribution: number;
};
export type Strategy = Schema["StrategyResponse"];
export type Analysis = {
  symbol: string;
  as_of: string | null;
  history_count: number;
  close: number | null;
  returns: Record<string, number | null>;
  moving_averages: Record<string, number | null>;
  distances: Record<string, number | null>;
  rsi: number | null;
  down_streak: number;
  volume_ratio: number | null;
  tags: string[];
  score: number | null;
  components: Component[];
  strategy_version: number;
};
export type Quote = {
  symbol: string;
  price: string | number;
  observed_at: string;
  source: string;
};
export type Instrument = {
  symbol: string;
  name: string;
  kind: Kind;
  sector: string;
};
export type Row = Instrument &
  Analysis & {
    quote: Quote;
    strategy_signal?: {
      eligible: boolean;
      reasons: string[];
      strategy_type: string;
      version: string | number;
      rank: number | null;
    };
  };
export type Screener = {
  rows: Row[];
  source: string;
  strategy: Strategy;
  generated_at: string;
};
export type Bar = {
  session: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};
export type Detail = Instrument & {
  analysis: Analysis;
  bars: Bar[];
  quote: Quote;
  source: string;
};
export type Order = {
  request_id: string;
  symbol: string;
  quantity: number;
  price: string;
  fee: string | null;
  at: string;
  source: string;
};
export type Portfolio = {
  cash: string;
  initial_cash: string;
  equity: string;
  holdings_value: string;
  profit: string;
  mode: string;
  source: string;
  buying_power?: string | null;
  settlement_cash?: string | null;
  realized_profit?: string | null;
  observed_at?: string;
  orders: Order[];
  positions: {
    symbol: string;
    name: string;
    quantity: number;
    average_price: string;
    price: string;
    value: string;
    profit: string;
  }[];
};

export type Readiness = Schema["Readiness"];
export type Context = Schema["ContextResponse"];
export type OrderState = Schema["OrderResponse"]["state"];
export type OrderEvent = {
  at?: string;
  state?: string;
  message?: string;
  kind?: string;
  [key: string]: unknown;
};
export type ManagedOrder = Schema["OrderResponse"] & {
  events: OrderEvent[];
  strategy_version?: number | null;
  pending_commands?: { id: string; kind: string; state: string }[];
};
export type Deployment = {
  version: number;
  enabled: boolean;
  strategy_type: "kospi_pullback" | "etf_trend";
  rules: Record<string, number | boolean>;
  model_id: string | null;
  allocation_policy: "single_active";
  updated_at: string;
};
export type IntentInput = Schema["OrderInput"];
export type OrderPage = {
  items: ManagedOrder[];
  next_cursor: string | null;
  total: number;
};
export type DataRecord = Record<string, unknown>;
export type AgentSignal = Schema["AgentSignalInput"];
export type AgentControl = Omit<
  Schema["AgentControlInput"],
  "expected_version"
> & {
  version: number;
};
export type TradingStatus = {
  mode: "local_simulation" | "kis_paper" | "kis_live";
  control: AgentControl;
  entries_enabled: boolean;
  execution_only: boolean;
  worker: DataRecord | null;
  signals: ManagedOrder[];
};
export type Research = Schema["ResearchResponse"] & {
  fundamentals?: DataRecord[];
  macro?: DataRecord[];
  features?: DataRecord[];
  readiness?: Readiness[];
  source_status?: DataRecord;
  real_validation_complete?: boolean;
};
export type Operations = {
  jobs: DataRecord[];
  reconciliations: DataRecord[];
  health: DataRecord | DataRecord[];
  entries_enabled: boolean;
};
export type Reports = Schema["ReportResponse"];
export type Watchlist = {
  items: DataRecord[];
  as_of: string | null;
  source: string;
};

export class ApiError extends Error {
  status: number;
  code: string;
  correlationId?: string;
  suggestedAction?: string;
  constructor(
    message: string,
    status = 0,
    code = "NETWORK_ERROR",
    correlationId?: string,
    suggestedAction?: string,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
    this.suggestedAction = suggestedAction;
  }
}

export async function request<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort(signal?.reason);
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) abort();
  const timer = setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await fetch(`/api${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers:
        body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new ApiError(
        typeof error.message === "string"
          ? error.message
          : typeof error.detail === "string"
            ? error.detail
            : `요청을 처리하지 못했습니다 (${response.status}).`,
        response.status,
        error.code ?? `HTTP_${response.status}`,
        error.correlation_id,
        error.suggested_action,
      );
    }
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof ApiError || signal?.aborted) throw error;
    throw new ApiError(
      controller.signal.aborted
        ? "응답 시간이 초과되었습니다. 주문은 결과를 조회해 확인하세요."
        : "서버와 통신하지 못했습니다. 연결 상태를 확인하세요.",
    );
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

type QuerySignal = { signal?: AbortSignal };
export const api = {
  context: ({ signal }: QuerySignal = {}) =>
    request<Context>("/context", undefined, signal),
  screener: ({ signal }: QuerySignal = {}) =>
    request<Screener>("/screener", undefined, signal),
  portfolio: ({ signal }: QuerySignal = {}) =>
    request<Portfolio>("/portfolio", undefined, signal),
  detail: (symbol: string, signal?: AbortSignal) =>
    request<Detail>(
      `/instruments/${encodeURIComponent(symbol)}`,
      undefined,
      signal,
    ),
  strategies: ({ signal }: QuerySignal = {}) =>
    request<{ versions: Strategy[] }>("/strategies", undefined, signal),
  saveStrategy: (strategy: Strategy) => {
    const { version, ...values } = strategy;
    return request<Strategy>("/strategies", {
      ...values,
      expected_version: version,
    });
  },
  buy: (symbol: string, quantity: number, request_id: string) =>
    request<Order>("/paper/buys", { symbol, quantity, request_id }),
  orders: (
    filters: {
      cursor?: string;
      status?: string;
      symbol?: string;
      limit?: number;
    } = {},
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters))
      if (value !== undefined && value !== "") params.set(key, String(value));
    return request<OrderPage>(`/orders?${params}`, undefined, signal);
  },
  order: (id: string, signal?: AbortSignal) =>
    request<ManagedOrder>(
      `/orders/${encodeURIComponent(id)}`,
      undefined,
      signal,
    ),
  intent: (input: IntentInput) =>
    request<ManagedOrder>("/order-intents", input),
  recoverIntent: (id: string, signal?: AbortSignal) =>
    request<ManagedOrder>(
      `/order-intents/by-client-id/${encodeURIComponent(id)}`,
      undefined,
      signal,
    ),
  submitIntent: (id: string) =>
    request<ManagedOrder>(
      `/order-intents/${encodeURIComponent(id)}/submit`,
      {},
    ),
  cancelOrder: (
    id: string,
    input: { client_request_id: string; quantity: number },
  ) => request<ManagedOrder>(`/orders/${encodeURIComponent(id)}/cancel`, input),
  amendOrder: (
    id: string,
    input: { client_request_id: string; quantity: number; limit_price: string },
  ) => request<ManagedOrder>(`/orders/${encodeURIComponent(id)}/amend`, input),
  operations: ({ signal }: QuerySignal = {}) =>
    request<Operations>("/operations", undefined, signal),
  runJob: (kind: string, payload: DataRecord = {}) =>
    request<DataRecord>("/operations/jobs", { kind, payload }),
  control: (entries_enabled: boolean) =>
    request<DataRecord>("/operations/control", { entries_enabled }),
  trading: ({ signal }: QuerySignal = {}) =>
    request<TradingStatus>("/trading", undefined, signal),
  tradingInstruments: ({ signal }: QuerySignal = {}) =>
    request<Pick<Instrument, "symbol" | "name" | "kind">[]>(
      "/trading/instruments",
      undefined,
      signal,
    ),
  tradingControl: (value: AgentControl) => {
    return request<AgentControl>("/trading/control", {
      expected_version: value.version,
      enabled: value.enabled,
      allowed_symbols: value.allowed_symbols,
      max_order_value: value.max_order_value,
      max_position_pct: value.max_position_pct,
      max_invested_pct: value.max_invested_pct,
    });
  },
  previewSignal: (value: AgentSignal) =>
    request<DataRecord>("/trading/signals/preview", value),
  submitSignal: (value: AgentSignal) =>
    request<ManagedOrder>("/trading/signals", value),
  signalStatus: (id: string) =>
    request<ManagedOrder>(`/trading/signals/${encodeURIComponent(id)}`),
  research: ({ signal }: QuerySignal = {}) =>
    request<Research>("/research", undefined, signal),
  importDataset: (payload: DataRecord) =>
    request<DataRecord>("/research/import", payload),
  experiment: (input: DataRecord) =>
    request<DataRecord>("/research/experiments", input),
  experimentDetail: (id: string, signal?: AbortSignal) =>
    request<DataRecord>(
      `/research/experiments/${encodeURIComponent(id)}`,
      undefined,
      signal,
    ),
  researchFeatures: (
    datasetId: string,
    asOf?: string,
    symbol?: string,
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams({ dataset_id: datasetId });
    if (asOf) params.set("as_of", asOf);
    if (symbol) params.set("symbol", symbol);
    return request<{
      dataset_id: string;
      as_of: string;
      rows: DataRecord[];
      coverage: DataRecord;
      source_kind: string;
    }>(`/research/features?${params}`, undefined, signal);
  },
  modelStatus: (id: string, status: string) =>
    request<DataRecord>(`/research/models/${encodeURIComponent(id)}/status`, {
      status,
    }),
  deployment: ({ signal }: QuerySignal = {}) =>
    request<Deployment>("/deployment", undefined, signal),
  saveDeployment: (value: Deployment) =>
    request<Deployment>("/deployment", {
      expected_version: value.version,
      strategy_type: value.strategy_type,
      rules: value.rules,
      model_id: value.model_id,
      enabled: value.enabled,
    }),
  reports: ({ signal }: QuerySignal = {}) =>
    request<Reports>("/reports", undefined, signal),
  watchlist: ({ signal }: QuerySignal = {}) =>
    request<Watchlist>("/watchlist", undefined, signal),
};

export const won = (value: string | number | null | undefined) =>
  value == null ||
  (typeof value === "string" && !value.trim()) ||
  !Number.isFinite(Number(value))
    ? "—"
    : Math.round(Number(value)).toLocaleString("ko-KR");
export const pct = (value: number | null | undefined) =>
  value == null || !Number.isFinite(value)
    ? "—"
    : `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
export const kindLabels: Record<Kind, string> = {
  kospi: "코스피",
  etf_kr: "국내 ETF",
  etf_us: "미국 지수 ETF",
};
export const modeLabel = (mode?: string) => {
  const labels: Record<string, string> = {
    local_simulation: "로컬 시뮬레이션",
    kis_paper: "한투 모의계좌",
    kis_live: "한투 실전계좌",
  };
  return labels[mode ?? ""] ?? "확인 중";
};
export const tagLabels: Record<string, string> = {
  review: "검토 기준 충족",
  uptrend: "상승 추세",
  downtrend: "하락 추세",
  mixed: "혼조",
  declining: "연속 하락",
  oversold: "과매도",
  overbought: "과매수",
  rebound: "반등 조건 충족",
  insufficient: "이력 부족",
};
export const reasonLabels: Record<string, string> = {
  insufficient_history: "분석 이력 부족",
  not_tradable: "거래 가능 상태 아님",
  not_kospi_common: "코스피 보통주 대상 아님",
  not_etf: "ETF 대상 아님",
  etf_metadata_or_eligibility: "ETF 정보·상품 적격 확인 필요",
  uptrend_required: "장기 상승 추세 미충족",
  invalid_atr: "유효한 ATR 없음",
  insufficient_liquidity: "거래대금 조건 미충족",
  medium_trend_required: "중기 상승 추세 미충족",
  outside_pullback_band: "눌림 범위 밖",
  outside_rsi_band: "RSI 범위 밖",
  recovery_not_confirmed: "종가 회복 미확인",
  financial_sector_quality_requires_separate_policy:
    "금융업 전용 재무 기준 필요",
  quality_missing_or_rejected: "재무 자료 부족·품질 조건 미충족",
  macro_missing: "매크로 자료 없음",
  macro_risk_off: "매크로 위험 한도 초과",
  trend_exit: "추세 이탈",
  close_stop: "종가 손절 조건",
  pullback_recovered: "눌림 회복",
  holding_expiry: "최대 보유 기간 도달",
};
