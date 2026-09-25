import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  BarChart3,
  Check,
  ChevronRight,
  CircleHelp,
  Coins,
  FlaskConical,
  LayoutDashboard,
  ListFilter,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Star,
  Wallet,
  X,
} from "lucide-react";
import { api, kindLabels, pct, tagLabels, won } from "./api";
import type { Bar, Detail, Kind, Row, Strategy } from "./api";

function ErrorBox({ error }: { error: Error }) {
  return (
    <div className="error-box" role="alert">
      {error.message} <span>백엔드 실행 상태를 확인해 주세요.</span>
    </div>
  );
}
function Change({ value }: { value: number | null | undefined }) {
  return (
    <span
      className={
        value == null || value === 0
          ? "neutral"
          : value > 0
            ? "positive"
            : "negative"
      }
    >
      {pct(value)}
    </span>
  );
}
function Badge({ tag }: { tag: string }) {
  return <span className={`badge ${tag}`}>{tagLabels[tag] ?? tag}</span>;
}
function Score({ value }: { value: number | null }) {
  return (
    <span className="score">
      <span className="score-track">
        <i style={{ width: `${value ?? 0}%` }} />
      </span>
      <b>{value == null ? "—" : value.toFixed(0)}</b>
    </span>
  );
}
function PageTitle({
  eyebrow,
  title,
  description,
  children,
}: {
  eyebrow: string;
  title: string;
  description: string;
  children?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {children}
    </div>
  );
}
function DataNote() {
  return (
    <div className="data-note">
      <FlaskConical size={17} />
      <span>
        <b>샘플 데이터로 살펴보는 연구실</b> · 가격은 생성된 예제이며 실제
        시세가 아닙니다. 계좌는 초기 자금 1,000만 원의 로컬 모의계좌입니다.
      </span>
    </div>
  );
}

export default function App() {
  const client = useQueryClient();
  const screener = useQuery({ queryKey: ["screener"], queryFn: api.screener });
  const [selected, setSelected] = useState<string | null>(null);
  const refresh = () => client.invalidateQueries();
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <NavLink to="/" className="brand">
          <span className="brand-mark">
            <BarChart3 size={23} />
          </span>
          <span>
            invest<span className="brand-light">-lab</span>
            <small>PERSONAL RESEARCH WORKSPACE</small>
          </span>
        </NavLink>
        <div className="nav-caption">WORKSPACE</div>
        <nav aria-label="주 메뉴">
          <NavLink to="/" end>
            <LayoutDashboard size={19} />
            대시보드
          </NavLink>
          <NavLink to="/market">
            <BarChart3 size={19} />
            종목 탐색
            <span className="nav-count">
              {screener.data?.rows.length ?? "—"}
            </span>
          </NavLink>
          <NavLink to="/strategies">
            <SlidersHorizontal size={19} />
            전략 관리
          </NavLink>
          <NavLink to="/activity">
            <Activity size={19} />
            모의투자 기록
          </NavLink>
        </nav>
        <div className="sidebar-card">
          <span className="tiny-label">현재 실행 환경</span>
          <strong>
            <i className="status-dot" />
            LOCAL SIMULATION
          </strong>
          <p>
            내 PC에서 전략을 검증하고
            <br />
            판단의 근거를 기록합니다.
          </p>
          <span className="version-label">실전 주문 연결 없음</span>
        </div>
        <div className="sidebar-bottom">
          <div className="avatar">WH</div>
          <div>
            Won-Hyuk-Lee<small>개인 투자 연구실</small>
          </div>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            내 워크스페이스 <ChevronRight size={14} />
            <span>국내 주식 · ETF</span>
          </div>
          <div className="topbar-right">
            <span className="local-label">
              <i className="status-dot" />
              로컬
            </span>
            <button
              className="icon-button"
              title="데이터 다시 읽기"
              aria-label="데이터 다시 읽기"
              onClick={refresh}
            >
              <RefreshCw size={17} />
            </button>
          </div>
        </header>
        <main>
          <DataNote />
          <Routes>
            <Route path="/" element={<Dashboard onSelect={setSelected} />} />
            <Route path="/market" element={<Market onSelect={setSelected} />} />
            <Route path="/strategies" element={<Strategies />} />
            <Route path="/activity" element={<ActivityPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
          <footer className="page-footer">
            <span>INVEST-LAB / RESEARCH FIRST</span>
            <span>
              모든 기간은 거래일 기준 · 매수 검토 점수는 수익 확률이 아닙니다.
            </span>
          </footer>
        </main>
      </div>
      {selected && (
        <InstrumentDialog symbol={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

function Dashboard({ onSelect }: { onSelect: (symbol: string) => void }) {
  const account = useQuery({ queryKey: ["portfolio"], queryFn: api.portfolio });
  const market = useQuery({ queryKey: ["screener"], queryFn: api.screener });
  if (account.error) return <ErrorBox error={account.error} />;
  if (market.error) return <ErrorBox error={market.error} />;
  const data = account.data;
  const rows = market.data?.rows ?? [];
  const ranking = [...rows]
    .sort((a, b) => (b.score ?? -1) - (a.score ?? -1))
    .slice(0, 5);
  const cashShare = data
    ? (Number(data.cash) / Number(data.equity)) * 100
    : 100;
  return (
    <>
      <PageTitle
        eyebrow="OVERVIEW"
        title="나의 투자 현황"
        description="자산의 변화와 오늘 확인할 종목을 한눈에 살펴보세요."
      >
        <NavLink className="button primary" to="/market">
          종목 탐색하기
          <ArrowRight size={16} />
        </NavLink>
      </PageTitle>
      <div className="metrics">
        <Metric
          title="총 평가자산"
          value={data ? won(data.equity) : "—"}
          icon={<Wallet size={19} />}
          note="현금 + 보유 종목 평가금액"
          featured
        />
        <Metric
          title="주문 가능 현금"
          value={data ? won(data.cash) : "—"}
          icon={<Coins size={19} />}
          note={`자산의 ${cashShare.toFixed(1)}% · 로컬 모의계좌`}
        />
        <Metric
          title="보유 종목 평가금액"
          value={data ? won(data.holdings_value) : "—"}
          icon={<BarChart3 size={19} />}
          note={`${data?.positions.length ?? 0}개 종목 보유`}
        />
        <Metric
          title="평가손익"
          value={
            data
              ? `${Number(data.profit) > 0 ? "+" : ""}${won(data.profit)}`
              : "—"
          }
          icon={<Activity size={19} />}
          note="보유 수량 기준 · 모의 매수 수수료 포함"
        />
      </div>
      <div className="dashboard-grid">
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>보유 포트폴리오</h2>
              <p>나의 자산 구성과 종목별 평가 현황</p>
            </div>
            <span className="subtle-pill">로컬 모의</span>
          </div>
          {data?.positions.length ? (
            <div className="table-scroll">
              <table className="holdings-table">
                <thead>
                  <tr>
                    <th>종목</th>
                    <th>보유 수량</th>
                    <th>평가금액</th>
                    <th>평가손익</th>
                  </tr>
                </thead>
                <tbody>
                  {data.positions.map((p) => (
                    <tr key={p.symbol}>
                      <td>
                        <button
                          className="text-button"
                          onClick={() => onSelect(p.symbol)}
                        >
                          {p.name}
                        </button>
                        <small>{p.symbol}</small>
                      </td>
                      <td>{p.quantity}주</td>
                      <td>{won(p.value)}원</td>
                      <td
                        className={
                          Number(p.profit) > 0
                            ? "positive"
                            : Number(p.profit) < 0
                              ? "negative"
                              : ""
                        }
                      >
                        {won(p.profit)}원
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="portfolio-empty">
              <div className="cash-ring">
                <Wallet size={29} />
                <span>현금 비중</span>
                <strong>
                  100<span>%</span>
                </strong>
              </div>
              <div>
                <h3>첫 번째 투자 판단을 기록해 보세요.</h3>
                <p>
                  종목을 탐색하고 근거를 확인한 다음
                  <br />
                  로컬 모의매수를 기록하면 여기에 표시됩니다.
                </p>
                <NavLink to="/market" className="inline-link">
                  종목 살펴보기 <ArrowRight size={15} />
                </NavLink>
              </div>
            </div>
          )}
          <div className="panel-foot">
            <span>
              <i className="legend-dot blue" /> 현금 {cashShare.toFixed(1)}%
            </span>
            <span>
              <i className="legend-dot violet" /> 보유 종목{" "}
              {(100 - cashShare).toFixed(1)}%
            </span>
          </div>
        </section>
        <section className="panel research-summary">
          <div className="panel-heading">
            <div>
              <h2>시그널 브리핑</h2>
              <p>선택한 전략으로 분류한 샘플 종목</p>
            </div>
            <Activity size={20} />
          </div>
          <div className="signal-counts">
            {[
              ["oversold", "과매도"],
              ["rebound", "반등 조건"],
              ["declining", "연속 하락"],
            ].map(([tag, label]) => (
              <div key={tag}>
                <span>{label}</span>
                <strong>
                  {rows.filter((r) => r.tags.includes(tag)).length}
                  <small>종목</small>
                </strong>
              </div>
            ))}
          </div>
          <div className="active-strategy">
            <div className="strategy-icon">
              <SlidersHorizontal size={19} />
            </div>
            <div>
              <small>탐색에 적용한 전략</small>
              <strong>{market.data?.strategy.name ?? "불러오는 중"}</strong>
              <span>
                버전 {market.data?.strategy.version ?? "—"} · 연구용 점수
              </span>
            </div>
          </div>
          <NavLink className="inline-link" to="/strategies">
            전략 설정 살펴보기 <ArrowRight size={15} />
          </NavLink>
        </section>
      </div>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>지금 살펴볼 종목</h2>
            <p>매수 검토 점수 순 · 샘플 관심 종목 {rows.length}개 기준</p>
          </div>
          <NavLink className="inline-link" to="/market">
            전체 보기
            <ArrowRight size={15} />
          </NavLink>
        </div>
        <StockTable rows={ranking} onSelect={onSelect} compact />
      </section>
    </>
  );
}

function Metric({
  title,
  value,
  note,
  icon,
  featured = false,
}: {
  title: string;
  value: string;
  note: string;
  icon: ReactNode;
  featured?: boolean;
}) {
  return (
    <section className={`metric ${featured ? "featured" : ""}`}>
      <div>
        <span>{title}</span>
        {icon}
      </div>
      <strong>
        {value}
        <small>원</small>
      </strong>
      <p>{note}</p>
    </section>
  );
}

function Market({ onSelect }: { onSelect: (symbol: string) => void }) {
  const query = useQuery({ queryKey: ["screener"], queryFn: api.screener });
  const [kind, setKind] = useState<Kind | "all">("all");
  const [search, setSearch] = useState("");
  const [tag, setTag] = useState("all");
  const [sort, setSort] = useState("score");
  if (query.error) return <ErrorBox error={query.error} />;
  const rows = (query.data?.rows ?? [])
    .filter(
      (r) =>
        (kind === "all" || r.kind === kind) &&
        (tag === "all" || r.tags.includes(tag)) &&
        (r.name.toLowerCase().includes(search.toLowerCase()) ||
          r.symbol.includes(search)),
    )
    .sort((a, b) =>
      sort === "score"
        ? (b.score ?? -1) - (a.score ?? -1)
        : sort === "down"
          ? b.down_streak - a.down_streak
          : (b.returns["30"] ?? -Infinity) - (a.returns["30"] ?? -Infinity),
    );
  return (
    <>
      <PageTitle
        eyebrow="MARKET EXPLORER"
        title="종목 탐색"
        description="코스피와 국내 상장 ETF를 같은 기준으로 읽고, 판단의 근거를 비교하세요."
      />
      <div className="market-overview">
        <div>
          <strong>
            {query.data?.rows.length ?? "—"}
            <small>종목</small>
          </strong>
          <span>초기 관심 종목 목록</span>
        </div>
        <div>
          <strong>
            7 · 30 · 90<small>거래일</small>
          </strong>
          <span>기간별 종가 등락률</span>
        </div>
        <div>
          <strong>
            90 · 120<small>일선</small>
          </strong>
          <span>중장기 추세와 이격도</span>
        </div>
        <div className="market-overview-note">
          <CircleHelp size={18} />
          <span>
            전체 코스피 목록이 아닌 초기 관심 목록입니다.
            <br />
            과매도 상태가 반등을 보장하지는 않습니다.
          </span>
        </div>
      </div>
      <section className="panel market-panel">
        <div className="market-toolbar">
          <div className="segment" aria-label="자산 유형">
            {(
              [
                ["all", "전체"],
                ["kospi", "코스피"],
                ["etf_kr", "국내 ETF"],
                ["etf_us", "미국 지수 ETF"],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                className={kind === value ? "selected" : ""}
                aria-pressed={kind === value}
                onClick={() => setKind(value)}
              >
                {label}
              </button>
            ))}
          </div>
          <label className="search-box">
            <Search size={17} />
            <input
              aria-label="종목 검색"
              placeholder="종목명 또는 코드 검색"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
        </div>
        <div className="filter-row">
          <div>
            <ListFilter size={16} />
            <select
              aria-label="종목 상태 필터"
              value={tag}
              onChange={(e) => setTag(e.target.value)}
            >
              <option value="all">모든 상태</option>
              {Object.entries(tagLabels).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <span className="result-count">{rows.length}개 종목</span>
          </div>
          <select
            aria-label="정렬 기준"
            value={sort}
            onChange={(e) => setSort(e.target.value)}
          >
            <option value="score">검토 점수 높은 순</option>
            <option value="down">연속 하락 많은 순</option>
            <option value="return">30일 등락률 높은 순</option>
          </select>
        </div>
        {query.isPending ? (
          <div className="empty">종목을 분석하고 있습니다…</div>
        ) : (
          <StockTable rows={rows} onSelect={onSelect} />
        )}
        <div className="table-note">
          분석 기준일 {query.data?.rows[0]?.as_of ?? "—"} · 수정주가 일봉을
          연결할 수 있는 구조 · 현재는 생성된 예제 데이터
        </div>
      </section>
    </>
  );
}

function StockTable({
  rows,
  onSelect,
  compact = false,
}: {
  rows: Row[];
  onSelect: (symbol: string) => void;
  compact?: boolean;
}) {
  const [favorites, setFavorites] = useState<string[]>(() => {
    try {
      const data = JSON.parse(localStorage.getItem("invest-favorites") ?? "[]");
      return Array.isArray(data)
        ? data.filter((x) => typeof x === "string")
        : [];
    } catch {
      return [];
    }
  });
  const toggle = (symbol: string) => {
    const next = favorites.includes(symbol)
      ? favorites.filter((x) => x !== symbol)
      : [...favorites, symbol];
    setFavorites(next);
    try {
      localStorage.setItem("invest-favorites", JSON.stringify(next));
    } catch {
      /* Favorites remain usable in memory. */
    }
  };
  return (
    <div className="table-scroll">
      <table className="stock-table">
        <thead>
          <tr>
            <th className="star-cell">
              <Star size={14} />
            </th>
            <th>종목</th>
            <th>
              가격 <small>샘플</small>
            </th>
            <th>7일</th>
            <th>30일</th>
            <th>90일</th>
            {!compact && (
              <>
                <th>
                  RSI <small>14</small>
                </th>
                <th>연속 하락</th>
                <th>120일선 대비</th>
              </>
            )}
            <th>상태</th>
            <th>검토 점수</th>
            <th aria-label="상세 보기" />
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.symbol}>
              <td className="star-cell">
                <button
                  className={`star-button ${favorites.includes(row.symbol) ? "on" : ""}`}
                  aria-label={`${row.name} 관심 종목`}
                  aria-pressed={favorites.includes(row.symbol)}
                  onClick={() => toggle(row.symbol)}
                >
                  <Star size={16} />
                </button>
              </td>
              <td>
                <button
                  className="stock-name"
                  onClick={() => onSelect(row.symbol)}
                >
                  {row.name}
                </button>
                <small>
                  {row.symbol}
                  <span>·</span>
                  {kindLabels[row.kind]}
                </small>
              </td>
              <td className="price-cell">
                {won(row.quote.price)}
                <small>원</small>
              </td>
              <td>
                <Change value={row.returns["7"]} />
              </td>
              <td>
                <Change value={row.returns["30"]} />
              </td>
              <td>
                <Change value={row.returns["90"]} />
              </td>
              {!compact && (
                <>
                  <td>{row.rsi?.toFixed(1) ?? "—"}</td>
                  <td>{row.down_streak ? `${row.down_streak}거래일` : "—"}</td>
                  <td>
                    <Change value={row.distances["120"]} />
                  </td>
                </>
              )}
              <td>
                <div className="badges">
                  {row.tags.slice(0, compact ? 1 : 2).map((tag) => (
                    <Badge key={tag} tag={tag} />
                  ))}
                </div>
              </td>
              <td>
                <Score value={row.score} />
              </td>
              <td>
                <button
                  className="icon-button"
                  onClick={() => onSelect(row.symbol)}
                  aria-label={`${row.name} 상세`}
                >
                  <ChevronRight size={16} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!rows.length && (
        <div className="empty">
          조건에 맞는 종목이 없습니다. 검색어나 상태 필터를 바꿔보세요.
        </div>
      )}
    </div>
  );
}

function Strategies() {
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  const query = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  if (query.error) return <ErrorBox error={query.error} />;
  return (
    <>
      <PageTitle
        eyebrow="STRATEGY STUDIO"
        title="전략 관리"
        description="가설을 설정으로 남기고, 변경할 때마다 새로운 버전으로 기록하세요."
      />
      {savedVersion && (
        <p className="success" role="status">
          <Check size={16} />
          전략 v{savedVersion}을 저장했습니다.
        </p>
      )}
      {query.data ? (
        <StrategyEditor
          key={query.data.versions[0].version}
          strategy={query.data.versions[0]}
          versions={query.data.versions}
          onSaved={setSavedVersion}
        />
      ) : (
        <div className="empty">전략을 불러오고 있습니다…</div>
      )}
    </>
  );
}

function StrategyEditor({
  strategy,
  versions,
  onSaved,
}: {
  strategy: Strategy;
  versions: Strategy[];
  onSaved: (version: number) => void;
}) {
  const [draft, setDraft] = useState(strategy);
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: api.saveStrategy,
    onSuccess: async (saved) => {
      onSaved(saved.version);
      await client.invalidateQueries({ queryKey: ["screener"] });
      await client.invalidateQueries({ queryKey: ["instrument"] });
      await client.invalidateQueries({ queryKey: ["strategies"] });
    },
  });
  const total = Object.values(draft.weights).reduce((sum, n) => sum + n, 0);
  const descriptions: Record<string, [string, string]> = {
    oversold: ["RSI 과매도", "RSI가 설정한 과매도 기준에 가까울수록 높은 점수"],
    pullback: [
      "30일선 대비 눌림",
      "30일선보다 낮은 정도를 0~10% 구간에서 점수화",
    ],
    trend: ["중장기 추세", "종가와 30·90·120일선의 상대적 위치를 평가"],
    rebound: ["반등 조건", "낮은 RSI 이후 종가 상승 + 거래량 1.2배 이상"],
  };
  const update = (field: keyof Strategy, value: string | number) =>
    setDraft({ ...draft, [field]: value });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    save.mutate(draft);
  };
  return (
    <div className="strategy-grid">
      <form className="panel strategy-form" onSubmit={submit}>
        <div className="panel-heading">
          <div>
            <h2>매수 검토 점수 설계</h2>
            <p>
              현재 버전 {strategy.version} · 변경 사항은 새 버전으로 저장됩니다.
            </p>
          </div>
          <span className="subtle-pill">가중합 모델</span>
        </div>
        <label className="field">
          전략 이름
          <input
            value={draft.name}
            maxLength={60}
            required
            onChange={(e) => update("name", e.target.value)}
          />
        </label>
        <div className="weight-heading">
          <h3>지표별 가중치</h3>
          <span className={total === 100 ? "valid" : "invalid"}>
            합계 {total} / 100
          </span>
        </div>
        <div className="weights">
          {Object.entries(draft.weights).map(([key, value]) => (
            <label className="weight-row" key={key}>
              <div>
                <strong>{descriptions[key][0]}</strong>
                <small>{descriptions[key][1]}</small>
              </div>
              <input
                aria-label={`${descriptions[key][0]} 가중치`}
                type="range"
                min="0"
                max="100"
                step="1"
                value={value}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    weights: {
                      ...draft.weights,
                      [key]: Number(e.target.value),
                    },
                  })
                }
              />
              <span>
                {value}
                <small>%</small>
              </span>
            </label>
          ))}
        </div>
        <div className="settings-grid">
          {(
            [
              ["oversold_rsi", "과매도 RSI 기준"],
              ["overbought_rsi", "과매수 RSI 기준"],
              ["review_threshold", "검토 기준 점수"],
              ["max_position_pct", "종목당 최대 보유 비중 (%)"],
            ] as const
          ).map(([key, label]) => (
            <label className="field" key={key}>
              {label}
              <input
                type="number"
                min={key === "review_threshold" ? 0 : 1}
                max={key.includes("rsi") ? 99 : 100}
                step="1"
                required
                value={draft[key]}
                onChange={(e) => update(key, Number(e.target.value))}
              />
            </label>
          ))}
        </div>
        <p className="form-note">
          초기 가중치는 검증 전 연구용 가설입니다. 점수 80은 상승 확률 80%를
          뜻하지 않습니다. 121거래일 미만 종목은 점수 산정에서 제외합니다.
        </p>
        {save.error && (
          <div className="error-box" role="alert">
            {save.error.message}
          </div>
        )}
        <div className="form-actions">
          <span>기존 버전은 그대로 보관됩니다.</span>
          <button
            className="button primary"
            disabled={total !== 100 || save.isPending}
          >
            {save.isPending ? "저장 중…" : "새 버전으로 저장"}
            <ArrowRight size={16} />
          </button>
        </div>
      </form>
      <aside>
        <section className="panel methodology">
          <div className="panel-heading">
            <h2>점수는 어떻게 계산하나요?</h2>
            <FlaskConical size={19} />
          </div>
          <p>각 지표를 0~100으로 환산한 뒤, 설정한 가중치를 곱해 합산합니다.</p>
          <div className="formula">검토 점수 = Σ (지표 점수 × 가중치)</div>
          <p>
            추세·과매도·반등은 동시에 나타날 수 있어 여러 상태를 함께
            표시합니다.
          </p>
          <p>
            ETF의 추종지수, 환헤지, 괴리율 등은 별도의 분석 항목으로 확장할
            예정입니다.
          </p>
        </section>
        <section className="panel versions">
          <div className="panel-heading">
            <h2>버전 기록</h2>
            <span>{versions.length}개</span>
          </div>
          {versions.map((v, i) => (
            <div className="version-row" key={v.version}>
              <span className="version-circle">{v.version}</span>
              <div>
                <strong>{v.name}</strong>
                <small>{Object.values(v.weights).join(" / ")} · 가중치</small>
              </div>
              {i === 0 && <span className="subtle-pill">현재</span>}
            </div>
          ))}
        </section>
      </aside>
    </div>
  );
}

function ActivityPage() {
  const query = useQuery({ queryKey: ["portfolio"], queryFn: api.portfolio });
  if (query.error) return <ErrorBox error={query.error} />;
  return (
    <>
      <PageTitle
        eyebrow="PAPER ACTIVITY"
        title="모의투자 기록"
        description="로컬에서 기록한 매수와 현금 변화를 확인하세요. 증권사 주문은 전송되지 않습니다."
      />
      <section className="panel">
        <div className="panel-heading">
          <h2>로컬 모의매수</h2>
          <span className="subtle-pill">
            {query.data?.orders.length ?? 0}건
          </span>
        </div>
        {query.data?.orders.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>시각</th>
                  <th>종목코드</th>
                  <th>수량</th>
                  <th>기록 가격</th>
                  <th>모의 수수료</th>
                  <th>데이터</th>
                </tr>
              </thead>
              <tbody>
                {query.data.orders.map((o) => (
                  <tr key={o.request_id}>
                    <td>{new Date(o.at).toLocaleString("ko-KR")}</td>
                    <td>{o.symbol}</td>
                    <td>{o.quantity}주</td>
                    <td>{won(o.price)}원</td>
                    <td>{won(o.fee)}원</td>
                    <td>샘플</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty large">
            <Activity size={32} />
            <h3>아직 기록된 주문이 없습니다.</h3>
            <p>종목 상세 화면에서 로컬 모의매수를 기록할 수 있습니다.</p>
            <NavLink to="/market" className="button secondary">
              종목 탐색으로 이동
              <ArrowRight size={16} />
            </NavLink>
          </div>
        )}
      </section>
    </>
  );
}

function InstrumentDialog({
  symbol,
  onClose,
}: {
  symbol: string;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const query = useQuery({
    queryKey: ["instrument", symbol],
    queryFn: () => api.detail(symbol),
  });
  return (
    <dialog
      ref={dialog}
      className="instrument-dialog"
      onCancel={onClose}
      aria-label="종목 상세"
    >
      <button
        className="dialog-close icon-button"
        aria-label="상세 닫기"
        onClick={onClose}
      >
        <X size={20} />
      </button>
      {query.error ? (
        <ErrorBox error={query.error} />
      ) : query.data ? (
        <InstrumentBody detail={query.data} />
      ) : (
        <div className="empty">종목을 불러오고 있습니다…</div>
      )}
    </dialog>
  );
}

function InstrumentBody({ detail }: { detail: Detail }) {
  const analysis = detail.analysis;
  const [quantity, setQuantity] = useState(1);
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
  const client = useQueryClient();
  const buy = useMutation({
    mutationFn: () => api.buy(detail.symbol, quantity, requestId),
    onSuccess: async () => {
      setRequestId(crypto.randomUUID());
      await client.invalidateQueries({ queryKey: ["portfolio"] });
    },
  });
  return (
    <>
      <div className="detail-heading">
        <div className="eyebrow">
          {kindLabels[detail.kind]} / {detail.sector}
        </div>
        <h2>
          {detail.name}
          <small>{detail.symbol}</small>
        </h2>
        <div className="detail-price">
          {won(detail.quote.price)}
          <small>원</small>
          <span>
            <Change value={analysis.returns["1"]} />
          </span>
        </div>
        <span className="sample-label">
          샘플 가격 · 분석 기준일 {analysis.as_of}
        </span>
        <div className="badges">
          {analysis.tags.map((t) => (
            <Badge tag={t} key={t} />
          ))}
        </div>
      </div>
      <PriceChart bars={detail.bars} />
      <div className="detail-metrics">
        {(["7", "30", "90"] as const).map((days) => (
          <div key={days}>
            <span>{days}거래일 등락률</span>
            <strong>
              <Change value={analysis.returns[days]} />
            </strong>
          </div>
        ))}
        <div>
          <span>RSI (14)</span>
          <strong>{analysis.rsi?.toFixed(1) ?? "—"}</strong>
        </div>
      </div>
      <div className="detail-columns">
        <section>
          <div className="section-line">
            <h3>매수 검토 점수</h3>
            <strong>
              {analysis.score?.toFixed(1) ?? "—"}
              <small> / 100</small>
            </strong>
          </div>
          {analysis.components.map((c) => (
            <div className="component-row" key={c.key}>
              <div>
                <span>{c.label}</span>
                <small>가중치 {c.weight}%</small>
              </div>
              <div className="contribution-track">
                <i style={{ width: `${c.value}%` }} />
              </div>
              <b>+{c.contribution.toFixed(1)}</b>
            </div>
          ))}
          <p className="form-note">
            전략 v{analysis.strategy_version} · 연구용 순위 점수이며 미래
            수익이나 반등 확률이 아닙니다.
          </p>
        </section>
        <section className="detail-technical">
          <h3>기술 지표</h3>
          {(["90", "120"] as const).map((days) => (
            <div key={days}>
              <span>{days}일 이동평균</span>
              <strong>
                {won(analysis.moving_averages[days])}원{" "}
                <small>
                  <Change value={analysis.distances[days]} />
                </small>
              </strong>
            </div>
          ))}
          <div>
            <span>연속 하락</span>
            <strong>{analysis.down_streak}거래일</strong>
          </div>
          <div>
            <span>직전 20일 평균 대비 거래량</span>
            <strong>{analysis.volume_ratio?.toFixed(2) ?? "—"}배</strong>
          </div>
        </section>
      </div>
      <form
        className="paper-order"
        onSubmit={(e) => {
          e.preventDefault();
          buy.mutate();
        }}
      >
        <div>
          <h3>로컬 모의매수 기록</h3>
          <p>
            증권사 전송 없이 현재 샘플 가격으로 기록합니다. 수수료 가정 0.015%.
          </p>
        </div>
        <label>
          수량
          <input
            aria-label="모의매수 수량"
            type="number"
            min="1"
            max="100000"
            required
            value={quantity}
            onChange={(e) => {
              buy.reset();
              setQuantity(Number(e.target.value));
              setRequestId(crypto.randomUUID());
            }}
          />
        </label>
        <button className="button primary" disabled={buy.isPending}>
          {buy.isPending ? "기록 중…" : "모의매수 기록"}
        </button>
      </form>
      {buy.error && (
        <p className="error-box" role="alert">
          {buy.error.message}
        </p>
      )}
      {buy.isSuccess && (
        <p className="success" role="status">
          <Check size={16} />
          로컬 모의계좌에 기록했습니다. 대시보드에서 확인할 수 있습니다.
        </p>
      )}
    </>
  );
}

function PriceChart({ bars }: { bars: Bar[] }) {
  const shown = bars.slice(-130);
  const series = [
    { period: 0, color: "#5265df", label: "종가" },
    { period: 90, color: "#e9a04b", label: "90일선" },
    { period: 120, color: "#97a3b8", label: "120일선" },
  ];
  const values = series.map((s) =>
    shown.map((bar, index) => {
      if (!s.period) return bar.close;
      const end = bars.length - shown.length + index + 1;
      if (end < s.period) return null;
      return (
        bars.slice(end - s.period, end).reduce((n, b) => n + b.close, 0) /
        s.period
      );
    }),
  );
  const all = values.flat().filter((x): x is number => x !== null);
  const min = Math.min(...all) * 0.98,
    max = Math.max(...all) * 1.02,
    range = max - min || 1;
  const y = (n: number) => 185 - ((n - min) / range) * 150;
  const x = (i: number) => 10 + (i / Math.max(1, shown.length - 1)) * 710;
  return (
    <div className="price-chart">
      <div className="chart-heading">
        <span>
          가격 흐름 <small>최근 130개 샘플 거래일</small>
        </span>
        <div>
          {series.map((s) => (
            <span key={s.period}>
              <i style={{ background: s.color }} />
              {s.label}
            </span>
          ))}
        </div>
      </div>
      <svg
        viewBox="0 0 800 225"
        role="img"
        aria-label="샘플 종가와 90일, 120일 이동평균선 차트"
      >
        {[0, 1, 2, 3].map((i) => (
          <g key={i}>
            <line
              x1="10"
              x2="725"
              y1={35 + i * 50}
              y2={35 + i * 50}
              stroke="#e9edf3"
              strokeDasharray="4 4"
            />
            <text x="735" y={39 + i * 50} fill="#9aa4b7" fontSize="11">
              {won(max - (range * i) / 3)}
            </text>
          </g>
        ))}
        {series.map((s, si) => (
          <path
            key={s.period}
            d={values[si]
              .map((v, i) =>
                v === null
                  ? ""
                  : `${i === 0 || values[si][i - 1] === null ? "M" : "L"}${x(i)},${y(v)}`,
              )
              .join(" ")}
            fill="none"
            stroke={s.color}
            strokeWidth={s.period ? 1.7 : 2.6}
            strokeLinejoin="round"
          />
        ))}
        {[0, Math.floor(shown.length / 2), shown.length - 1].map((i) => (
          <text
            key={i}
            x={x(i)}
            y="215"
            textAnchor={
              i === 0 ? "start" : i === shown.length - 1 ? "end" : "middle"
            }
            fill="#9aa4b7"
            fontSize="11"
          >
            {shown[i]?.session}
          </text>
        ))}
      </svg>
    </div>
  );
}
