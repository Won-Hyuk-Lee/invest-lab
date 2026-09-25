from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Purchase:
    cash_after: Decimal
    quantity_after: int
    average_price_after: Decimal
    fee: Decimal


def plan_purchase(
    *,
    cash: Decimal,
    equity: Decimal,
    old_quantity: int,
    old_average: Decimal,
    quantity: int,
    price: Decimal,
    max_position_pct: float,
) -> Purchase:
    """Local ledger policy. Fees are an explicit research assumption, not KIS pricing."""
    if type(quantity) is not int or quantity <= 0 or not price.is_finite() or price <= 0:
        raise ValueError("가격과 수량을 확인하세요.")
    total = price * quantity
    fee = (total * Decimal("0.00015")).quantize(Decimal("1"))
    if total + fee > cash:
        raise ValueError("로컬 모의계좌의 주문 가능 현금이 부족합니다.")
    new_quantity = old_quantity + quantity
    if price * new_quantity > (equity - fee) * Decimal(str(max_position_pct)) / 100:
        raise ValueError("전략에 설정한 종목당 최대 보유 비중을 초과합니다.")
    average = (old_average * old_quantity + total + fee) / new_quantity
    return Purchase(cash - total - fee, new_quantity, average, fee)
