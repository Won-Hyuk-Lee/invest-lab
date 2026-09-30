"""Local order gateway for decisions made by an external AI or script."""

from datetime import datetime
from uuid import UUID

from invest_lab.domain.errors import TradingBlocked, VersionConflict
from invest_lab.domain.orders import OrderRequest, decimal


class AgentTradingService:
    def __init__(self, execution, repository, clock, queue_submit):
        self.execution, self.repo, self.clock = execution, repository, clock
        self.queue_submit = queue_submit
        if self.repo.document("agent_control") is None:
            self.repo.put_document(
                "agent_control",
                {
                    "version": 1,
                    "enabled": False,
                    "allowed_symbols": [],
                    "max_order_value": "100000",
                    "max_position_pct": 20,
                    "max_invested_pct": 50,
                },
            )

    def status(self):
        return {
            "mode": self.repo.mode,
            "control": self.repo.document("agent_control"),
            "entries_enabled": (self.repo.document("control") or {}).get("entries_enabled", False),
            "worker": self.repo.document("worker"),
            "signals": [o for o in self.repo.all_orders() if o["context"].get("origin") == "agent"][
                -50:
            ][::-1],
            "live_orders_supported": True,
        }

    def configure(self, payload):
        expected = payload["expected_version"]
        enabled = payload["enabled"]
        symbols = payload["allowed_symbols"]
        if type(enabled) is not bool or not isinstance(symbols, list):
            raise ValueError("연결 설정을 확인하세요.")
        known = {i.symbol for i in self.execution.instruments()}
        if any(
            not isinstance(s, str)
            or not s.isascii()
            or not s.isalnum()
            or (enabled and s not in known)
            for s in symbols
        ):
            raise ValueError("허용 종목은 현재 종목 마스터에서 확인된 코드여야 합니다.")
        if len(symbols) != len(set(symbols)) or len(symbols) > 50:
            raise ValueError("허용 종목은 중복 없이 최대 50개입니다.")
        if enabled and not symbols:
            raise ValueError("자동 주문을 허용할 종목을 먼저 지정하세요.")
        amount = decimal(payload["max_order_value"], positive=True)
        risk = {
            key: float(decimal(payload[key], positive=True))
            for key in ("max_position_pct", "max_invested_pct")
        }
        if any(value > 100 for value in risk.values()):
            raise ValueError("보유/투자 비중은 0 초과 100 이하입니다.")
        with self.repo.execution_lock():
            old = self.repo.document("agent_control")
            if type(expected) is not int or expected != old["version"]:
                raise VersionConflict("외부 주문 설정이 변경되었습니다. 다시 조회하세요.")
            if enabled and (self.repo.document("control") or {}).get(
                "restore_reconciliation_required"
            ):
                raise TradingBlocked("복원 후 계좌 대조와 신규 진입 재개를 먼저 확인하세요.")
            result = {
                "version": old["version"] + 1,
                "enabled": enabled,
                "allowed_symbols": symbols,
                "max_order_value": str(amount),
                **risk,
                "updated_at": self.clock.now().isoformat(),
            }
            self.repo.put_document("agent_control", result)
            return result

    def _parse(self, payload):
        if (
            not isinstance(payload, dict)
            or not {"source", "reason", "expires_at"} <= payload.keys()
        ):
            raise ValueError("판단 JSON에는 source, reason, expires_at가 필요합니다.")
        value = dict(payload)
        metadata = {key: value.pop(key) for key in ("source", "reason", "expires_at")}
        if value.keys() - {
            "client_request_id",
            "symbol",
            "side",
            "quantity",
            "order_type",
            "limit_price",
        }:
            raise ValueError("판단에는 주문 필드 외의 서버 설정을 넣을 수 없습니다.")
        for key, maximum in (("source", 80), ("reason", 1000)):
            if not isinstance(metadata[key], str) or not 1 <= len(metadata[key].strip()) <= maximum:
                raise ValueError(f"{key}는 비어 있지 않은 {maximum}자 이하 문자열입니다.")
        expires = datetime.fromisoformat(metadata["expires_at"])
        if expires.tzinfo is None:
            raise ValueError("expires_at에는 시간대가 필요합니다.")
        metadata["expires_at"] = expires.isoformat()
        request = OrderRequest(**value)
        return request.payload(), metadata

    def _validate(self, request, metadata, policy):
        remaining = (
            datetime.fromisoformat(metadata["expires_at"]) - self.clock.now()
        ).total_seconds()
        if not 0 < remaining <= 300:
            raise TradingBlocked(
                "판단 유효기간은 현재부터 최대 5분이며 만료된 판단은 실행하지 않습니다."
            )
        if request["symbol"] not in policy["allowed_symbols"]:
            raise TradingBlocked("허용 목록에 없는 종목입니다.")
        quote = self.execution._quote(request["symbol"])
        price = decimal(quote["price"], positive=True)
        if request["order_type"] == "market" and self.repo.mode != "local_simulation":
            if not quote.get("upper_limit"):
                raise TradingBlocked("시장가 주문 한도 검사에 필요한 상한가가 없습니다.")
            price = max(price, decimal(quote["upper_limit"], positive=True))
        if request["limit_price"] is not None:
            price = max(price, decimal(request["limit_price"], positive=True))
        amount = price * request["quantity"]
        if amount > decimal(policy["max_order_value"]):
            raise TradingBlocked("한 주문의 금액 한도를 초과합니다.")
        return {"estimated_value": str(amount), "quote": quote}

    def preview(self, payload):
        request, metadata = self._parse(payload)
        policy = self.repo.document("agent_control")
        return {
            "request": request,
            **metadata,
            **self._validate(request, metadata, policy),
            "submitted": False,
            "execution_enabled": policy["enabled"],
            "note": (
                "형식·유효기간·허용 종목·금액만 검사합니다. 잔고/장 시간 등은 실행 시 재검사합니다."
            ),
        }

    def submit(self, payload):
        request, metadata = self._parse(payload)
        with self.repo.execution_lock():
            previous = self.repo.find_intent(request["client_request_id"])
            if previous:
                if previous["context"].get("origin") != "agent" or any(
                    previous["context"].get(k) != v for k, v in metadata.items()
                ):
                    raise VersionConflict("같은 판단 ID를 다른 출처/근거에 사용할 수 없습니다.")
                # Validate immutable order content, but never requeue an existing signal.
                self.execution.register(request)
                return self.repo.get_order(previous["id"])
            policy = self.repo.document("agent_control")
            if not policy["enabled"]:
                raise TradingBlocked("외부 판단 자동 주문이 중지되어 있습니다.")
            self._validate(request, metadata, policy)
            order = self.execution.register(
                request,
                decision_context={
                    "origin": "agent",
                    "agent_control_version": policy["version"],
                    **metadata,
                    "risk_policy": {k: policy[k] for k in ("max_position_pct", "max_invested_pct")},
                },
            )
            return self.queue_submit(order["id"])

    def find(self, client_id):
        UUID(client_id)
        order = self.repo.find_intent(client_id)
        if not order or order["context"].get("origin") != "agent":
            raise LookupError("등록된 외부 판단이 없습니다.")
        return order
