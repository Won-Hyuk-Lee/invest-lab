import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, modeLabel, won } from "../api";
import type {
  AgentControl,
  AgentSignal,
  DataRecord,
  ManagedOrder,
} from "../api";
import { ErrorNotice, Loading, PageHeader, useWorkspace } from "./shared";
import { stateLabels } from "./orders";

export function TradingPage() {
  const client = useQueryClient();
  const workspace = useWorkspace();
  const status = useQuery({
    queryKey: ["trading"],
    queryFn: api.trading,
    refetchInterval: 5000,
  });
  const account = useQuery({
    queryKey: ["portfolio"],
    queryFn: api.portfolio,
    refetchInterval: 15000,
  });
  const instruments = useQuery({
    queryKey: ["trading-instruments"],
    queryFn: api.tradingInstruments,
  });
  const [draft, setDraft] = useState<AgentControl | null>(null);
  const [symbols, setSymbols] = useState("");
  const [text, setText] = useState("");
  const [checked, setChecked] = useState<AgentSignal | null>(null);
  const [preview, setPreview] = useState<DataRecord | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [result, setResult] = useState<ManagedOrder | null>(null);
  const currentResult =
    status.data?.signals.find((o) => o.id === result?.id) ?? result;
  const [uncertainId, setUncertainId] = useState<string | null>(null);
  const pendingKey = workspace.data
    ? `invest-agent:${workspace.data.mode}:${workspace.data.account_id}`
    : null;
  const keepPending = (signal: AgentSignal | null) => {
    if (!pendingKey) return;
    try {
      if (signal) localStorage.setItem(pendingKey, JSON.stringify(signal));
      else localStorage.removeItem(pendingKey);
    } catch {
      /* The durable server intent remains queryable by its ID. */
    }
    setUncertainId(signal?.client_request_id ?? null);
  };
  useEffect(() => {
    try {
      const value = pendingKey && localStorage.getItem(pendingKey);
      if (value) {
        const signal = JSON.parse(value) as AgentSignal;
        setUncertainId(signal.client_request_id);
        setText(JSON.stringify(signal, null, 2));
      } else setUncertainId(null);
    } catch {
      setUncertainId(null);
    }
  }, [pendingKey]);
  const policy = status.data?.control;
  useEffect(() => {
    if (policy) {
      setDraft(policy);
      setSymbols(policy.allowed_symbols.join(", "));
      setChecked(null);
      setPreview(null);
    }
    // Same-version polling preserves an unsaved limit draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [policy?.version]);
  const invalidate = () => {
    void client.invalidateQueries();
  };
  const save = useMutation({
    mutationFn: api.tradingControl,
    onSuccess: invalidate,
  });
  const entries = useMutation({
    mutationFn: api.control,
    onSuccess: invalidate,
  });
  const inspect = useMutation({
    mutationFn: api.previewSignal,
    onSuccess: (value, signal) => {
      setPreview(value);
      setChecked(signal);
    },
  });
  const send = useMutation({
    mutationFn: api.submitSignal,
    onMutate: (signal) => {
      keepPending(signal);
    },
    onSuccess: (value) => {
      setResult(value);
      setChecked(null);
      keepPending(null);
      invalidate();
    },
    onError: (failure) => {
      setChecked(null);
      if (
        failure instanceof ApiError &&
        failure.status >= 400 &&
        failure.status < 500
      )
        keepPending(null);
    },
  });
  const recover = useMutation({
    mutationFn: api.signalStatus,
    onSuccess: (value) => {
      setResult(value);
      keepPending(null);
      invalidate();
    },
    onError: (failure) => {
      if (failure instanceof ApiError && failure.status === 404) {
        keepPending(null);
        setError(
          new Error(
            "이 판단 ID는 등록되지 않았습니다. 내용을 다시 검사하세요.",
          ),
        );
      }
    },
  });
  const example = () => {
    if (uncertainId) return;
    const signal: AgentSignal = {
      client_request_id: crypto.randomUUID(),
      symbol:
        draft?.allowed_symbols[0] ?? instruments.data?.[0]?.symbol ?? "005930",
      side: "buy",
      quantity: 1,
      order_type: "market",
      limit_price: null,
      source: "local-agent",
      reason: "연결 흐름 확인용 판단",
      expires_at: new Date(Date.now() + 120000).toISOString(),
    };
    setText(JSON.stringify(signal, null, 2));
    setChecked(null);
    setPreview(null);
    setResult(null);
    setError(null);
    inspect.reset();
    send.reset();
    recover.reset();
  };
  return (
    <>
      <PageHeader title="로컬 매매 도구" eyebrow="LOCAL TRADING">
        AI·외부 스크립트가 판단하고, 이 앱이 한도 검사와 주문·체결 관리를
        수행합니다.
      </PageHeader>
      <div className="metrics">
        <section className="panel padded-panel">
          <span>총 평가자산</span>
          <h2>{won(account.data?.equity)}원</h2>
        </section>
        <section className="panel padded-panel">
          <span>현금</span>
          <h2>{won(account.data?.cash)}원</h2>
        </section>
        <section className="panel padded-panel">
          <span>외부 자동 주문</span>
          <h2>{policy?.enabled ? "허용" : "중지"}</h2>
        </section>
        <section className="panel padded-panel">
          <span>작업자</span>
          <h2>
            {workspace.data?.readiness?.find((r) => r.name === "worker")?.ready
              ? "실행 중"
              : "실행 필요"}
          </h2>
        </section>
      </div>
      <ErrorNotice error={account.error} />
      <ErrorNotice error={status.error} />
      <section className="panel padded-panel">
        <h2>보유 포트폴리오</h2>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>종목</th>
                <th>수량</th>
                <th>평가금액</th>
              </tr>
            </thead>
            <tbody>
              {account.data?.positions.map((p) => (
                <tr key={p.symbol}>
                  <td>
                    {p.name} · {p.symbol}
                  </td>
                  <td>{p.quantity}주</td>
                  <td>{won(p.value)}원</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {account.isPending && <Loading>계좌 조회 중…</Loading>}
        {account.data && !account.data.positions.length && (
          <p>보유 종목이 없습니다.</p>
        )}
      </section>
      <section className="panel padded-panel">
        <h2>외부 판단 연결 설정</h2>
        <p>
          허용한 종목과 한도 안에서만 자동 주문합니다. 설정을 변경하면 대기 중인
          이전 판단은 실행되지 않습니다.
        </p>
        {draft && (
          <>
            <fieldset disabled={save.isPending}>
              <div className="page-controls">
                <label className="field">
                  허용 종목 코드
                  <input
                    value={symbols}
                    onChange={(e) => setSymbols(e.target.value)}
                    placeholder="005930, 069500"
                  />
                </label>
                <label className="field">
                  주문당 최대 금액 (원)
                  <input
                    type="number"
                    min="1"
                    value={draft.max_order_value}
                    onChange={(e) =>
                      setDraft({ ...draft, max_order_value: e.target.value })
                    }
                  />
                </label>
                <label className="field">
                  종목별 최대 비중 (%)
                  <input
                    type="number"
                    min="0.01"
                    max="100"
                    value={draft.max_position_pct}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        max_position_pct: Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label className="field">
                  전체 최대 투자 비중 (%)
                  <input
                    type="number"
                    min="0.01"
                    max="100"
                    value={draft.max_invested_pct}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        max_invested_pct: Number(e.target.value),
                      })
                    }
                  />
                </label>
              </div>
              <div className="page-controls">
                <button
                  className="button secondary"
                  onClick={() =>
                    save.mutate({
                      ...draft,
                      allowed_symbols: symbols.split(/[\s,]+/).filter(Boolean),
                    })
                  }
                >
                  설정 저장
                </button>
                <button
                  className="button primary"
                  onClick={() =>
                    save.mutate({
                      ...draft,
                      enabled: true,
                      allowed_symbols: symbols.split(/[\s,]+/).filter(Boolean),
                    })
                  }
                >
                  저장하고 외부 자동 주문 허용
                </button>
                <button
                  className="button secondary"
                  disabled={!policy?.enabled}
                  onClick={() =>
                    policy && save.mutate({ ...policy, enabled: false })
                  }
                >
                  외부 자동 주문 중지
                </button>
              </div>
            </fieldset>
            <p>
              신규 매수: {status.data?.entries_enabled ? "허용" : "중지"} ·{" "}
              {modeLabel(workspace.data?.mode)}
            </p>
            <button
              className="button secondary"
              disabled={entries.isPending || !status.data}
              onClick={() => entries.mutate(!status.data!.entries_enabled)}
            >
              {status.data?.entries_enabled
                ? "신규 매수 중지"
                : "신규 매수 허용"}
            </button>
          </>
        )}
        <ErrorNotice error={save.error} />
        <ErrorNotice error={entries.error} />
        <details>
          <summary>현재 사용 가능한 종목</summary>
          <p>
            {instruments.data
              ?.map((i) => `${i.symbol} ${i.name}`)
              .join(" / ") || "등록된 종목 없음"}
          </p>
        </details>
      </section>
      <section className="panel padded-panel">
        <h2>AI·외부 판단 시험</h2>
        <p>
          외부 프로그램은 동일한 JSON을 API로 보내면 됩니다. 판단 ID는
          재사용하고, 유효기간은 최대 5분으로 지정합니다.
        </p>
        <button
          className="button secondary"
          disabled={!!uncertainId || send.isPending}
          onClick={example}
        >
          새 판단 예시 만들기
        </button>
        <div className="field">
          <label htmlFor="agent-signal">판단 JSON</label>
          <textarea
            id="agent-signal"
            rows={14}
            value={text}
            disabled={send.isPending || !!uncertainId}
            onChange={(e) => {
              setText(e.target.value);
              setChecked(null);
              setPreview(null);
            }}
          />
        </div>
        <div className="page-controls">
          <button
            className="button secondary"
            disabled={
              !text || inspect.isPending || send.isPending || !!uncertainId
            }
            onClick={() => {
              setError(null);
              setChecked(null);
              setPreview(null);
              try {
                const value: unknown = JSON.parse(text);
                if (!value || typeof value !== "object" || Array.isArray(value))
                  throw new Error("판단은 JSON 객체여야 합니다.");
                inspect.mutate(value as AgentSignal);
              } catch (e) {
                setError(
                  e instanceof Error ? e : new Error("JSON을 확인하세요."),
                );
              }
            }}
          >
            전송 없이 검사
          </button>
          <button
            className="button primary"
            disabled={
              !checked || !policy?.enabled || send.isPending || !!uncertainId
            }
            onClick={() => checked && send.mutate(checked)}
          >
            검사한 판단으로 주문 요청
          </button>
        </div>
        <ErrorNotice error={error} />
        <ErrorNotice error={inspect.error} />
        <ErrorNotice error={send.error} />
        {preview && (
          <p role="status">
            검사 완료 · 예상 주문금액 {won(String(preview.estimated_value))}원 ·
            아직 주문하지 않았습니다.
          </p>
        )}
        {uncertainId && (
          <div className="warning-note">
            <p>
              응답이 확인되지 않았습니다. 새 판단을 만들기 전에 같은 ID의 등록
              결과를 조회하세요.
            </p>
            <code>{uncertainId}</code>
            <button
              className="button secondary"
              disabled={recover.isPending}
              onClick={() => recover.mutate(uncertainId)}
            >
              판단 결과 조회
            </button>
            <ErrorNotice error={recover.error} />
          </div>
        )}
        {currentResult && (
          <p role="status">
            {stateLabels[currentResult.state]} ·{" "}
            <Link to={`/orders/${currentResult.id}`}>주문 상세 확인</Link>
          </p>
        )}
      </section>
      <section className="panel padded-panel">
        <h2>최근 외부 판단</h2>
        {!status.data?.signals.length && <p>등록된 외부 판단이 없습니다.</p>}
        {status.data?.signals.map((order) => (
          <p key={order.id}>
            <Link to={`/orders/${order.id}`}>
              {order.symbol} · {order.side === "buy" ? "매수" : "매도"}{" "}
              {order.quantity}주 · {stateLabels[order.state]}
            </Link>
          </p>
        ))}
      </section>
    </>
  );
}
