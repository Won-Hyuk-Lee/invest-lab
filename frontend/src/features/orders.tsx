import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError, won } from "../api";
import type { IntentInput, ManagedOrder, OrderState } from "../api";
import {
  ErrorNotice,
  Loading,
  PageHeader,
  RefreshNote,
  timestamp,
  useWorkspace,
} from "./shared";

export const stateLabels: Record<OrderState, string> = {
  registered: "등록됨 · 미전송",
  sending: "전송 중",
  acknowledged: "접수 확인",
  unknown: "결과 확인 필요",
  rejected: "거절",
  partially_filled: "부분 체결",
  filled: "체결 완료",
  cancelled: "잔량 취소",
  expired: "만료",
};
const terminal = (order: ManagedOrder) =>
  ["filled", "cancelled", "rejected", "expired"].includes(order.state);
const commandPending = (order: ManagedOrder) =>
  order.pending_commands?.some(
    (command) =>
      ![
        "succeeded",
        "completed",
        "confirmed",
        "acknowledged",
        "rejected",
        "failed",
      ].includes(command.state),
  ) ?? false;
const storageKey = (scope: string, symbol: string) =>
  `invest-intent:${scope}:${symbol}`;
function storedId(key: string) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function keepId(key: string, id: string | null) {
  try {
    if (id) localStorage.setItem(key, id);
    else localStorage.removeItem(key);
  } catch {
    /* Server order history remains the recovery source. */
  }
}

export function OrderTicket({
  symbol,
  price,
  strategyVersion,
}: {
  symbol: string;
  price?: string | number | null;
  strategyVersion?: number;
}) {
  const workspace = useWorkspace();
  const scope = workspace.data
    ? `${workspace.data.mode}:${workspace.data.account_id}`
    : "unavailable";
  const key = storageKey(scope, symbol);
  const client = useQueryClient();
  const [clientId, setClientId] = useState<string | null>(() => storedId(key));
  const [order, setOrder] = useState<ManagedOrder | null>(null);
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState("1");
  const [orderType, setOrderType] = useState<"limit" | "market">("limit");
  const [limitPrice, setLimitPrice] = useState(String(price ?? ""));
  const [uncertain, setUncertain] = useState(false);
  useEffect(() => {
    setClientId(storedId(key));
    setOrder(null);
    setUncertain(false);
  }, [key]);
  useEffect(() => {
    const changed = (event: StorageEvent) => {
      if (event.key === key && event.newValue) setClientId(event.newValue);
    };
    window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
  }, [key]);
  const recovery = useQuery({
    queryKey: ["intent", clientId],
    queryFn: ({ signal }) => api.recoverIntent(clientId!, signal),
    enabled: Boolean(clientId),
    retry: false,
    refetchInterval: clientId && (!order || !terminal(order)) ? 3_000 : false,
  });
  useEffect(() => {
    if (recovery.data) {
      setOrder(recovery.data);
      setUncertain(false);
    }
  }, [recovery.data]);
  const register = useMutation({
    mutationFn: api.intent,
    onSuccess: (saved) => {
      setOrder(saved);
      setUncertain(false);
      void client.invalidateQueries({ queryKey: ["orders"] });
    },
    onError: (error) => {
      if (
        !(error instanceof ApiError) ||
        error.status === 0 ||
        error.status >= 500
      )
        setUncertain(true);
    },
  });
  const submit = useMutation({
    mutationFn: api.submitIntent,
    onSuccess: (saved) => {
      setOrder(saved);
      setUncertain(saved.state === "unknown");
      void client.invalidateQueries();
    },
    onError: () => {
      setUncertain(true);
      void recovery.refetch();
    },
  });
  const locked = Boolean(clientId) || register.isPending || submit.isPending;
  const begin = (event: FormEvent) => {
    event.preventDefault();
    if (locked || !workspace.data || workspace.error) return;
    const existing = storedId(key);
    if (existing) {
      setClientId(existing);
      return;
    }
    const id = crypto.randomUUID();
    keepId(key, id);
    setClientId(id);
    setUncertain(false);
    const input: IntentInput = {
      client_request_id: id,
      symbol,
      side,
      quantity: Number(quantity),
      order_type: orderType,
      limit_price: orderType === "limit" ? limitPrice : null,
      strategy_version: strategyVersion ?? null,
    };
    register.mutate(input);
  };
  const reset = () => {
    keepId(key, null);
    setClientId(null);
    setOrder(null);
    setUncertain(false);
    register.reset();
    submit.reset();
  };
  const definiteRegistrationFailure =
    register.error instanceof ApiError &&
    register.error.status >= 400 &&
    register.error.status < 500 &&
    register.error.status !== 409 &&
    !order;
  return (
    <section className="order-ticket" aria-label={`${symbol} 주문 입력`}>
      <h3>
        {workspace.data?.mode === "kis_paper"
          ? "한투 모의 주문"
          : "로컬 모의 주문"}
      </h3>
      <p className="form-note">
        주문 내용을 등록한 뒤 전송합니다. 접수와 체결은 별도로 확인합니다.
      </p>
      {workspace.error && <ErrorNotice error={workspace.error} />}
      <form onSubmit={begin}>
        <fieldset
          disabled={locked || !workspace.data || Boolean(workspace.error)}
          className="ticket-fields"
        >
          <label className="field">
            매매 구분
            <select
              aria-label="매매 구분"
              value={side}
              onChange={(e) => setSide(e.target.value as "buy" | "sell")}
            >
              <option value="buy">매수</option>
              <option value="sell">매도</option>
            </select>
          </label>
          <label className="field">
            주문 유형
            <select
              aria-label="주문 유형"
              value={orderType}
              onChange={(e) =>
                setOrderType(e.target.value as "limit" | "market")
              }
            >
              <option value="limit">지정가</option>
              <option value="market">시장가</option>
            </select>
          </label>
          <label className="field">
            주문 수량
            <input
              aria-label="주문 수량"
              type="number"
              min="1"
              max="100000"
              step="1"
              required
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
            />
          </label>
          {orderType === "limit" && (
            <label className="field">
              지정 가격 (원)
              <input
                aria-label="지정 가격"
                type="number"
                min="1"
                step="1"
                required
                value={limitPrice}
                onChange={(e) => setLimitPrice(e.target.value)}
              />
            </label>
          )}
        </fieldset>
        {!locked && (
          <>
            <p className="form-note">
              예상 주문금액{" "}
              {won(
                Number(quantity) *
                  Number(orderType === "limit" ? limitPrice : price),
              )}
              원 · 수수료 별도
              {orderType === "market" &&
                " · 시장가 체결 가격은 달라질 수 있습니다"}
            </p>
            <button
              className="button primary"
              disabled={
                !workspace.data ||
                Boolean(workspace.error) ||
                (side === "buy" && !workspace.data.entries_enabled)
              }
            >
              주문 내용 등록
            </button>
            {side === "buy" &&
              workspace.data &&
              !workspace.data.entries_enabled && (
                <p className="form-note">신규 진입이 중지되어 있습니다.</p>
              )}
          </>
        )}
      </form>
      {(register.isPending || (clientId && !order && recovery.isPending)) && (
        <Loading>
          주문 등록 결과를 확인하고 있습니다. 입력은 고정됩니다.
        </Loading>
      )}
      <ErrorNotice error={register.error ?? submit.error} />
      {clientId && !order && !register.isPending && !recovery.isPending && (
        <div className="warning-note" role="status">
          {definiteRegistrationFailure
            ? "주문 내용이 거부되었습니다. 내용을 다시 입력할 수 있습니다."
            : "기존 요청을 확인해야 합니다. 아직 전송 결과를 확정할 수 없습니다."}
          <button
            className="button secondary"
            onClick={() => void recovery.refetch()}
          >
            기존 요청 조회
          </button>
          {definiteRegistrationFailure && (
            <button className="button secondary" onClick={reset}>
              내용 다시 입력
            </button>
          )}
          <Link to="/orders">주문 기록 열기</Link>
        </div>
      )}
      {order && (
        <>
          <OrderSummary order={order} />
          {order.state === "registered" &&
            !uncertain &&
            !commandPending(order) &&
            !submit.isSuccess && (
              <button
                className="button primary"
                disabled={submit.isPending || Boolean(workspace.error)}
                onClick={() => submit.mutate(order.id)}
              >
                {submit.isPending ? "전송 중…" : "등록한 주문 전송"}
              </button>
            )}
          {order.state === "registered" &&
            (commandPending(order) || submit.isSuccess) && (
              <p role="status">
                전송 작업을 요청했습니다. 작업자 처리 결과를 확인 중입니다.
              </p>
            )}
          <Link
            className="inline-link"
            to={`/orders/${encodeURIComponent(order.id)}`}
          >
            주문 상세·체결 확인
          </Link>
          {terminal(order) && (
            <button className="button secondary" onClick={reset}>
              별도 새 주문 입력
            </button>
          )}
        </>
      )}
      {(uncertain || order?.state === "unknown") && (
        <div className="warning-note" role="alert">
          전송 결과를 확인 중입니다. 새 주문으로 재전송하지 않습니다.
          <button
            className="button secondary"
            onClick={() => void recovery.refetch()}
          >
            결과 다시 조회
          </button>
        </div>
      )}
    </section>
  );
}

export function OrderSummary({ order }: { order: ManagedOrder }) {
  return (
    <div className="order-summary">
      <strong>
        {order.symbol} · {order.side === "buy" ? "매수" : "매도"} ·{" "}
        {stateLabels[order.state] ?? order.state}
      </strong>
      <p>
        주문 {order.quantity}주 / 체결 {order.filled_quantity}주 / 취소{" "}
        {order.cancelled_quantity}주 / 남은 수량 {order.remaining_quantity}주
      </p>
      <small>
        {timestamp(order.created_at)} ·{" "}
        {order.order_type === "market"
          ? "시장가"
          : `${won(order.limit_price)}원 지정가`}
      </small>
      {order.filled_quantity > 0 && (
        <p className="form-note">
          확인된 체결 금액{" "}
          {won(order.filled_amount) === "—"
            ? "미확인"
            : `${won(order.filled_amount)}원`}{" "}
          · 비용{" "}
          {typeof order.fee === "string" || typeof order.fee === "number"
            ? won(order.fee) === "—"
              ? "미확인"
              : `${won(order.fee)}원`
            : "미확인"}
        </p>
      )}
      {order.error && <p className="error-box">{order.error}</p>}
    </div>
  );
}

export function OrdersPage() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const symbol = params.get("symbol") ?? "";
  const cursor = params.get("cursor") ?? "";
  const query = useQuery({
    queryKey: ["orders", status, symbol, cursor],
    queryFn: ({ signal }) =>
      api.orders({ status, symbol, cursor, limit: 20 }, signal),
    refetchInterval: 5_000,
  });
  const filter = (name: string, value: string) => {
    const next = new URLSearchParams(params);
    next.delete("cursor");
    if (value) next.set(name, value);
    else next.delete(name);
    setParams(next);
  };
  return (
    <>
      <PageHeader title="주문과 체결" eyebrow="ORDER DESK">
        등록·전송·체결·대조 결과를 구분해 확인합니다.
      </PageHeader>
      <section className="panel padded-panel">
        <div className="page-controls">
          <label className="field">
            상태
            <select
              value={status}
              onChange={(e) => filter("status", e.target.value)}
            >
              <option value="">전체 상태</option>
              {Object.entries(stateLabels).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            종목 코드
            <input
              value={symbol}
              maxLength={16}
              onChange={(e) => filter("symbol", e.target.value.toUpperCase())}
              placeholder="005930 또는 0048K0"
            />
          </label>
        </div>
        {!query.data && query.isPending ? (
          <Loading />
        ) : !query.data ? (
          <ErrorNotice error={query.error} />
        ) : (
          <>
            <RefreshNote
              fetching={query.isFetching}
              error={query.error}
              updatedAt={query.dataUpdatedAt}
            />
            <p className="form-note">
              조회 범위 전체 {query.data.total}건 · 현재 페이지{" "}
              {query.data.items.length}건
            </p>
            {!query.data.items.length ? (
              <div className="empty">조건에 맞는 주문이 없습니다.</div>
            ) : (
              <div
                className="table-scroll"
                tabIndex={0}
                role="region"
                aria-label="주문 목록"
              >
                <table>
                  <thead>
                    <tr>
                      <th>시각</th>
                      <th>종목</th>
                      <th>매매</th>
                      <th>상태</th>
                      <th>주문/체결/잔량</th>
                      <th>상세</th>
                    </tr>
                  </thead>
                  <tbody>
                    {query.data.items.map((o) => (
                      <tr key={o.id}>
                        <td>{timestamp(o.created_at)}</td>
                        <td>{o.symbol}</td>
                        <td>{o.side === "buy" ? "매수" : "매도"}</td>
                        <td>{stateLabels[o.state] ?? o.state}</td>
                        <td>
                          {o.quantity} / {o.filled_quantity} /{" "}
                          {o.remaining_quantity}
                        </td>
                        <td>
                          <Link to={`/orders/${encodeURIComponent(o.id)}`}>
                            확인
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="page-controls">
              {cursor && (
                <button
                  className="button secondary"
                  onClick={() => filter("cursor", "")}
                >
                  첫 페이지
                </button>
              )}
              {query.data.next_cursor && (
                <button
                  className="button secondary"
                  onClick={() => {
                    const next = new URLSearchParams(params);
                    next.set("cursor", query.data!.next_cursor!);
                    setParams(next);
                  }}
                >
                  다음 페이지
                </button>
              )}
            </div>
          </>
        )}
      </section>
    </>
  );
}

export function OrderDetailPage() {
  const { id = "" } = useParams();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["order", id],
    queryFn: ({ signal }) => api.order(id, signal),
    refetchInterval: 3_000,
  });
  const [quantity, setQuantity] = useState("1");
  const [price, setPrice] = useState("");
  const [actionUncertain, setActionUncertain] = useState(false);
  const mutation = useMutation({
    mutationFn: ({
      action,
      requestId,
    }: {
      action: "cancel" | "amend";
      requestId: string;
    }) =>
      action === "cancel"
        ? api.cancelOrder(id, {
            client_request_id: requestId,
            quantity: Number(quantity),
          })
        : api.amendOrder(id, {
            client_request_id: requestId,
            quantity: query.data!.remaining_quantity,
            limit_price: price,
          }),
    onSuccess: (order) => {
      client.setQueryData(["order", id], order);
      setActionUncertain(true);
      void client.invalidateQueries();
    },
    onError: (error) => {
      if (
        !(error instanceof ApiError) ||
        error.status === 0 ||
        error.status >= 500
      )
        setActionUncertain(true);
    },
  });
  useEffect(() => {
    setActionUncertain(Boolean(storedId(`invest-order-action:${id}`)));
    const changed = (event: StorageEvent) => {
      if (event.key === `invest-order-action:${id}` && event.newValue)
        setActionUncertain(true);
    };
    window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
  }, [id]);
  useEffect(() => {
    const requestId = storedId(`invest-order-action:${id}`);
    const order = query.data;
    if (!requestId || !order) return;
    const confirmed = order.events.some(
      (event) =>
        (event.command_id === requestId ||
          event.client_request_id === requestId) &&
        [
          "succeeded",
          "completed",
          "confirmed",
          "acknowledged",
          "rejected",
          "failed",
        ].includes(String(event.command_state ?? "")),
    );
    if (confirmed || terminal(order)) {
      keepId(`invest-order-action:${id}`, null);
      setActionUncertain(false);
    }
  }, [query.data, id]);
  const act = (action: "cancel" | "amend") => {
    if (
      mutation.isPending ||
      actionUncertain ||
      !query.data ||
      query.error ||
      commandPending(query.data)
    )
      return;
    if (storedId(`invest-order-action:${id}`)) {
      setActionUncertain(true);
      return;
    }
    const requestId = crypto.randomUUID();
    keepId(`invest-order-action:${id}`, requestId);
    mutation.mutate(
      { action, requestId },
      {
        onSuccess: (result) => {
          if (terminal(result)) keepId(`invest-order-action:${id}`, null);
        },
        onError: (error) => {
          if (
            error instanceof ApiError &&
            error.status >= 400 &&
            error.status < 500
          )
            keepId(`invest-order-action:${id}`, null);
        },
      },
    );
  };
  return (
    <>
      <PageHeader title="주문 상세" eyebrow="ORDER TIMELINE">
        주문 의도와 접수·체결·취소 기록을 확인합니다.
      </PageHeader>
      <Link to="/orders" className="inline-link">
        전체 주문으로
      </Link>
      <section className="panel padded-panel">
        {!query.data ? (
          query.isPending ? (
            <Loading />
          ) : (
            <ErrorNotice error={query.error} />
          )
        ) : (
          <>
            <OrderSummary order={query.data} />
            <RefreshNote
              fetching={query.isFetching}
              error={query.error}
              updatedAt={query.dataUpdatedAt}
            />
            <div className="form-note">
              요청 식별자 {query.data.client_request_id} · 전략 버전{" "}
              {query.data.strategy_version ?? "수동 주문"}
            </div>
            {query.data.state === "registered" && (
              <p className="form-note">
                아직 증권사에 전송되지 않았습니다. 해당 종목의 주문 입력에서
                기존 요청을 복구해 전송할 수 있습니다.
              </p>
            )}
            {query.data.remaining_quantity > 0 &&
              ["acknowledged", "partially_filled"].includes(
                query.data.state,
              ) && (
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    act("amend");
                  }}
                >
                  <fieldset
                    disabled={
                      mutation.isPending ||
                      actionUncertain ||
                      commandPending(query.data) ||
                      Boolean(query.error)
                    }
                    className="ticket-fields"
                  >
                    <label className="field">
                      취소할 수량
                      <input
                        type="number"
                        min="1"
                        step="1"
                        max={query.data.remaining_quantity}
                        required
                        value={quantity}
                        onChange={(e) => setQuantity(e.target.value)}
                      />
                    </label>
                    <label className="field">
                      정정 가격 (원)
                      <input
                        type="number"
                        min="1"
                        step="1"
                        required
                        value={price}
                        onChange={(e) => setPrice(e.target.value)}
                      />
                    </label>
                    <button
                      className="button secondary"
                      type="button"
                      disabled={
                        !Number.isInteger(Number(quantity)) ||
                        Number(quantity) < 1 ||
                        Number(quantity) > query.data.remaining_quantity
                      }
                      onClick={() => act("cancel")}
                    >
                      해당 잔량 취소
                    </button>
                    <button className="button primary" type="submit">
                      남은 {query.data.remaining_quantity}주 가격 정정 요청
                    </button>
                  </fieldset>
                </form>
              )}
            <ErrorNotice error={mutation.error} />
            {actionUncertain && (
              <div className="warning-note" role="alert">
                정정·취소 요청 결과가 미확정입니다. 반복 전송을 막았습니다. 운영
                화면에서 주문 대조를 실행해 확인하세요.
                <Link to="/operations">운영 상태 열기</Link>
              </div>
            )}
            <h2>이벤트 기록</h2>
            {query.data.events.length ? (
              <ol className="event-list">
                {query.data.events.map((event, index) => (
                  <li key={index}>
                    <time>{timestamp(event.at)}</time>
                    <strong>
                      {event.state
                        ? (stateLabels[event.state as OrderState] ??
                          String(event.state))
                        : String(event.kind ?? "이벤트")}
                    </strong>
                    <p>{String(event.message ?? "상태가 갱신되었습니다.")}</p>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="empty">아직 이벤트가 없습니다.</p>
            )}
          </>
        )}
      </section>
    </>
  );
}
