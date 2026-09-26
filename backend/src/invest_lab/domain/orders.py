from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal
from uuid import UUID

from invest_lab.domain.errors import TradingBlocked

TERMINAL = frozenset({"filled", "cancelled", "rejected", "expired"})
ACTIVE = frozenset({"sending", "acknowledged", "unknown", "partially_filled"})


@dataclass(frozen=True)
class OrderRequest:
    client_request_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: int
    order_type: Literal["limit", "market"] = "limit"
    limit_price: str | None = None
    strategy_version: int | None = None

    def __post_init__(self):
        UUID(self.client_request_id)
        if not self.symbol or not self.symbol.isascii() or not self.symbol.isalnum():
            raise ValueError("종목 식별자를 확인하세요.")
        if self.side not in {"buy", "sell"} or self.order_type not in {"limit", "market"}:
            raise ValueError("주문 방향/유형을 확인하세요.")
        if type(self.quantity) is not int or not 1 <= self.quantity <= 100000:
            raise ValueError("수량은 1~100000의 정수여야 합니다.")
        if self.order_type == "limit":
            decimal(self.limit_price, positive=True)
        elif self.limit_price is not None:
            raise ValueError("시장가 주문에는 지정가를 입력하지 않습니다.")

    def payload(self) -> dict:
        return asdict(self)


def decimal(value, *, positive=False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("금액을 확인하세요.")
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise ValueError("금액을 확인하세요.") from error
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError("금액은 유한한 양수여야 합니다.")
    return result


def valid_price(price, tick, lower, upper, side: str) -> Decimal:
    price, tick = decimal(price, positive=True), decimal(tick, positive=True)
    rounded = (price / tick).to_integral_value(
        rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING
    ) * tick
    if rounded <= 0 or (lower and rounded < decimal(lower)) or (upper and rounded > decimal(upper)):
        raise TradingBlocked("주문 가격이 허용 가격 범위를 벗어났습니다.")
    return rounded


def execution_state(quantity: int, filled: int, cancelled: int, rejected: int = 0) -> str:
    if min(quantity, filled, cancelled, rejected) < 0 or filled + cancelled + rejected > quantity:
        raise ValueError("주문/체결/취소 수량이 일치하지 않습니다.")
    if filled == quantity:
        return "filled"
    if filled + cancelled == quantity:
        return "cancelled"
    if rejected == quantity:
        return "rejected"
    return "partially_filled" if filled else "acknowledged"
