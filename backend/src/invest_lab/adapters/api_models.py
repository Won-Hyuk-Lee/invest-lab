"""Public JSON contracts. Secrets and broker account identifiers are never API fields."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Response(BaseModel):
    model_config = ConfigDict(extra="allow")


class QuoteResponse(Response):
    symbol: str
    price: str
    observed_at: str
    source: str


class MarketRow(Response):
    symbol: str
    name: str
    kind: str
    sector: str
    as_of: str | None = None
    history_count: int
    close: float | None = None
    returns: dict[str, float | None]
    moving_averages: dict[str, float | None]
    distances: dict[str, float | None]
    rsi: float | None = None
    down_streak: int
    volume_ratio: float | None = None
    tags: list[str]
    score: float | None = None
    components: list[dict[str, Any]]
    strategy_version: int
    quote: QuoteResponse


class ScreenResponse(Response):
    rows: list[MarketRow]
    source: str
    strategy: dict[str, Any]
    generated_at: str
    period_basis: Literal["trading_sessions"]


class Position(Response):
    symbol: str
    name: str
    quantity: int
    average_price: str
    price: str
    value: str
    profit: str


class AccountResponse(Response):
    cash: str
    initial_cash: str | None = None
    holdings_value: str
    equity: str
    profit: str
    positions: list[Position]
    orders: list[dict[str, Any]]
    mode: Literal["local_simulation", "kis_paper", "kis_live"]
    source: str


class StrategyResponse(Response):
    version: int
    name: str
    weights: dict[str, int]
    oversold_rsi: float
    overbought_rsi: float
    review_threshold: float
    max_position_pct: float


class StrategyVersions(Response):
    versions: list[StrategyResponse]


class Readiness(Response):
    name: str
    ready: bool
    message: str


class ContextResponse(Response):
    mode: Literal["local_simulation", "kis_paper", "kis_live"]
    account_id: str
    account_alias: str
    broker_connected: bool
    live_orders_enabled: bool
    entries_enabled: bool
    readiness: list[Readiness]
    server_time: str


class AgentSignalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    client_request_id: UUID
    symbol: str = Field(pattern=r"^[A-Z0-9]{6}$")
    side: Literal["buy", "sell"]
    quantity: int = Field(strict=True, ge=1, le=100000)
    order_type: Literal["limit", "market"] = "limit"
    limit_price: str | None = None
    source: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=1000)
    expires_at: str


class AgentControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    expected_version: int = Field(strict=True, ge=1)
    enabled: bool = Field(strict=True)
    allowed_symbols: list[str] = Field(max_length=50)
    max_order_value: str
    max_position_pct: float = Field(gt=0, le=100)
    max_invested_pct: float = Field(gt=0, le=100)


class OrderResponse(Response):
    id: str
    client_request_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: int
    order_type: Literal["limit", "market"]
    limit_price: str | None = None
    state: Literal[
        "registered",
        "sending",
        "acknowledged",
        "unknown",
        "rejected",
        "partially_filled",
        "filled",
        "cancelled",
        "expired",
    ]
    filled_quantity: int
    filled_amount: str
    cancelled_quantity: int
    remaining_quantity: int
    events: list[dict[str, Any]]
    created_at: str
    error: str | None = None


class OrderList(Response):
    items: list[OrderResponse]
    next_cursor: str | None
    total: int


class JobResponse(Response):
    id: str
    kind: str
    state: str
    created_at: str


class OperationsResponse(Response):
    jobs: list[JobResponse]
    reconciliations: list[dict[str, Any]]
    health: ContextResponse
    entries_enabled: bool


class ResearchResponse(Response):
    datasets: list[dict[str, Any]]
    experiments: list[dict[str, Any]]
    models: list[dict[str, Any]]


class ReportResponse(Response):
    daily: list[dict[str, Any]]
    monthly: list[dict[str, Any]]
    baseline: dict[str, Any] | None
    observation_complete: bool


class WatchlistResponse(Response):
    items: list[dict[str, Any]]
    as_of: str | None = None
    source: str


class DetailResponse(Response):
    symbol: str
    name: str
    kind: str
    sector: str
    analysis: dict[str, Any]
    quote: QuoteResponse
    bars: list[dict[str, Any]]
    source: str


class HealthResponse(Response):
    status: str
    mode: str
    source: str
    broker_connected: bool
    live_orders_enabled: bool


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class OrderInput(StrictInput):
    client_request_id: UUID
    symbol: str = Field(pattern=r"^[A-Z0-9]{6}$")
    side: Literal["buy", "sell"]
    quantity: int = Field(strict=True, ge=1, le=100000)
    order_type: Literal["limit", "market"] = "limit"
    limit_price: str | None = None
    strategy_version: int | None = Field(default=None, ge=1)


class CancelInput(StrictInput):
    client_request_id: UUID
    quantity: int = Field(strict=True, ge=1, le=100000)


class AmendInput(CancelInput):
    limit_price: str


class JobInput(StrictInput):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ControlInput(StrictInput):
    entries_enabled: bool = Field(strict=True)


class DeploymentInput(StrictInput):
    expected_version: int = Field(strict=True, ge=1)
    strategy_type: Literal["kospi_pullback", "etf_trend"]
    rules: dict[str, Any]
    model_id: str | None = None
    enabled: bool = Field(strict=True)


class ModelStatusInput(StrictInput):
    status: Literal["candidate", "shadow", "selected", "rejected"]


class ErrorResponse(BaseModel):
    code: str
    message: str
    correlation_id: str
    suggested_action: str
    detail: str
