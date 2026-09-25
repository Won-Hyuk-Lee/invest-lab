export type Kind = "kospi" | "etf_kr" | "etf_us";
export type Component = {
  key: string;
  label: string;
  value: number;
  weight: number;
  contribution: number;
};
export type Strategy = {
  version: number;
  name: string;
  weights: Record<string, number>;
  oversold_rsi: number;
  overbought_rsi: number;
  review_threshold: number;
  max_position_pct: number;
};
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
export type Row = Instrument & Analysis & { quote: Quote };
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
  fee: string;
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

async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(
      typeof error.detail === "string"
        ? error.detail
        : `요청을 처리하지 못했습니다 (${response.status}).`,
    );
  }
  return response.json() as Promise<T>;
}

export const api = {
  screener: () => request<Screener>("/screener"),
  portfolio: () => request<Portfolio>("/portfolio"),
  detail: (symbol: string) =>
    request<Detail>(`/instruments/${encodeURIComponent(symbol)}`),
  strategies: () => request<{ versions: Strategy[] }>("/strategies"),
  saveStrategy: (strategy: Strategy) => {
    const { version, ...values } = strategy;
    return request<Strategy>("/strategies", {
      ...values,
      expected_version: version,
    });
  },
  buy: (symbol: string, quantity: number, request_id: string) =>
    request<Order>("/paper/buys", { symbol, quantity, request_id }),
};

export const won = (value: string | number | null | undefined) =>
  value == null ? "—" : Math.round(Number(value)).toLocaleString("ko-KR");
export const pct = (value: number | null | undefined) =>
  value == null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
export const kindLabels: Record<Kind, string> = {
  kospi: "코스피",
  etf_kr: "국내 ETF",
  etf_us: "미국 지수 ETF",
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
