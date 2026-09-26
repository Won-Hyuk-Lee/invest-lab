import os
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from invest_lab.adapters.api_models import (
    AccountResponse,
    AmendInput,
    CancelInput,
    ContextResponse,
    ControlInput,
    DeploymentInput,
    DetailResponse,
    ErrorResponse,
    HealthResponse,
    JobInput,
    JobResponse,
    ModelStatusInput,
    OperationsResponse,
    OrderInput,
    OrderList,
    OrderResponse,
    ReportResponse,
    ResearchResponse,
    ScreenResponse,
    StrategyResponse,
    StrategyVersions,
    WatchlistResponse,
)
from invest_lab.application.service import Workspace
from invest_lab.bootstrap import build_workspace
from invest_lab.domain.errors import TradingBlocked, VersionConflict


class StrategyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    expected_version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=60)
    weights: dict[str, Annotated[int, Field(strict=True, ge=0, le=100)]]
    oversold_rsi: float = Field(gt=0, lt=100)
    overbought_rsi: float = Field(gt=0, lt=100)
    review_threshold: float = Field(ge=0, le=100)
    max_position_pct: float = Field(gt=0, le=100)


class PaperBuyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z0-9]{6}$")
    quantity: int = Field(strict=True, ge=1, le=100000)
    request_id: UUID


def create_app(workspace: Workspace | None = None, runtime=None) -> FastAPI:
    normal_start = workspace is None
    workspace = workspace or build_workspace()
    if runtime is None and normal_start:
        from invest_lab.adapters.runtime import build_runtime

        runtime = build_runtime(workspace, root=Path(__file__).resolve().parents[4])
    app = FastAPI(
        title="invest-lab local API",
        version="0.2.0",
        responses={
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    app.state.runtime = runtime

    def wire(value):
        return jsonable_encoder(value, custom_encoder={Decimal: str})

    @app.exception_handler(VersionConflict)
    async def conflict(request, error):
        return failure(409, "VERSION_CONFLICT", str(error), "reload_latest_preserve_draft")

    @app.exception_handler(TradingBlocked)
    async def blocked(request, error):
        return failure(409, "TRADING_BLOCKED", str(error), "reconcile_order")

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return failure(422, "INVALID_INPUT", str(error), "fix_input")

    @app.exception_handler(LookupError)
    async def missing(request, error):
        return failure(404, "NOT_FOUND", str(error), "retry_read")

    def failure(status, code, message, action):
        return JSONResponse(
            status_code=status,
            content={
                "code": code,
                "message": message,
                "detail": message,
                "suggested_action": action,
                "correlation_id": str(uuid4()),
            },
        )

    origins = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ]
    extra_origin = os.getenv("INVEST_UI_ORIGIN")
    if extra_origin:
        parsed_origin = urlparse(extra_origin)
        if (
            parsed_origin.scheme != "http"
            or parsed_origin.hostname not in {"127.0.0.1", "localhost"}
            or parsed_origin.path
        ):
            raise ValueError("추가 화면 origin은 로컬 HTTP 주소여야 합니다.")
        origins.append(extra_origin)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        # Localhost services still need to reject browser writes from unrelated websites.
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin not in origins:
            return JSONResponse({"detail": "허용되지 않은 요청 출처입니다."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if not request.url.path.startswith(("/api", "/docs", "/redoc", "/openapi")):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'self'; form-action 'self'"
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health", response_model=HealthResponse)
    def health():
        if runtime:
            return {"status": "ok", **runtime.context()}
        return {
            "status": "ok",
            "mode": "local_simulation",
            "source": workspace.market.source,
            "broker_connected": False,
            "live_orders_enabled": False,
        }

    @app.get("/api/screener", response_model=ScreenResponse)
    def screener():
        if runtime:
            snapshot = runtime.store.document("screen")
            if snapshot is None:
                raise TradingBlocked(
                    "수집/분석된 시세가 없습니다. 운영 화면에서 데이터 수집을 실행하세요."
                )
            return snapshot
        return wire(workspace.screen())

    @app.get("/api/instruments/{symbol}", response_model=DetailResponse)
    def detail(symbol: str):
        try:
            return wire(workspace.detail(symbol))
        except LookupError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/portfolio", response_model=AccountResponse)
    def portfolio():
        return wire(runtime.account() if runtime else workspace.account())

    @app.get("/api/strategies", response_model=StrategyVersions)
    def strategies():
        return {"versions": [asdict(s) for s in workspace.strategies.versions()]}

    @app.post("/api/strategies", status_code=201, response_model=StrategyResponse)
    def save_strategy(body: StrategyInput):
        try:
            saved = asdict(
                workspace.save_strategy(
                    body.model_dump(exclude={"expected_version"}), body.expected_version
                )
            )
            if runtime:
                runtime.store.enqueue("analyze")
            return saved
        except VersionConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/paper/buys", status_code=201)
    def paper_buy(body: PaperBuyInput):
        try:
            if runtime:
                order = runtime.execution.register(
                    {
                        "symbol": body.symbol,
                        "quantity": body.quantity,
                        "client_request_id": str(body.request_id),
                        "side": "buy",
                        "order_type": "market",
                    }
                )
                return runtime.queue_submit(order["id"])
            return workspace.buy(body.symbol, body.quantity, str(body.request_id))
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    if runtime:

        @app.get("/api/context", response_model=ContextResponse)
        def context():
            return runtime.context()

        @app.get("/api/orders", response_model=OrderList)
        def orders(
            cursor: str | None = None,
            limit: int = 50,
            status: str | None = None,
            symbol: str | None = None,
            start: str | None = None,
            end: str | None = None,
        ):
            return runtime.store.list_orders(
                cursor=cursor, limit=limit, status=status, symbol=symbol, start=start, end=end
            )

        @app.post("/api/order-intents", status_code=201, response_model=OrderResponse)
        def register_order(body: OrderInput):
            return runtime.execution.register(body.model_dump(mode="json"))

        @app.get("/api/order-intents/by-client-id/{client_id}", response_model=OrderResponse)
        def find_order(client_id: UUID):
            order = runtime.store.find_intent(str(client_id))
            if not order:
                raise LookupError("주문 의도가 아직 등록되지 않았습니다.")
            return runtime.order(order["id"])

        @app.get("/api/orders/{order_id}", response_model=OrderResponse)
        def order_detail(order_id: UUID):
            return runtime.order(str(order_id))

        @app.post("/api/order-intents/{order_id}/submit", response_model=OrderResponse)
        def submit_order(order_id: UUID):
            return runtime.queue_submit(str(order_id))

        @app.post("/api/orders/{order_id}/cancel", response_model=OrderResponse)
        def cancel_order(order_id: UUID, body: CancelInput):
            return runtime.queue_command(str(order_id), body.model_dump(mode="json"))

        @app.post("/api/orders/{order_id}/amend", response_model=OrderResponse)
        def amend_order(order_id: UUID, body: AmendInput):
            return runtime.queue_command(str(order_id), body.model_dump(mode="json"), amend=True)

        @app.get("/api/operations", response_model=OperationsResponse)
        def operations():
            return runtime.operations()

        @app.post("/api/operations/control", response_model=dict)
        def set_control(body: ControlInput):
            return runtime.control(body.entries_enabled)

        @app.post("/api/operations/jobs", status_code=202, response_model=JobResponse)
        def queue_job(body: JobInput):
            from invest_lab.adapters.runtime import JOB_KINDS

            if body.kind not in JOB_KINDS:
                raise ValueError("지원하지 않는 작업입니다.")
            return runtime.store.enqueue(body.kind, body.payload)

        @app.get("/api/deployment", response_model=dict)
        def deployment():
            return runtime.store.document("deployment")

        @app.post("/api/deployment", response_model=dict)
        def save_deployment(body: DeploymentInput):
            result = runtime.save_deployment(body.model_dump())
            runtime.store.enqueue("analyze")
            return result

        @app.get("/api/watchlist", response_model=WatchlistResponse)
        def watchlist():
            return runtime.store.document("watchlist") or {
                "items": [],
                "as_of": None,
                "source": workspace.market.source,
            }

        @app.get("/api/research", response_model=ResearchResponse)
        def research_overview():
            return runtime.research.overview()

        @app.post("/api/research/import", status_code=201, response_model=dict)
        def import_dataset(body: dict):
            return runtime.research.import_dataset(body)

        @app.get("/api/research/features", response_model=dict)
        def research_features(dataset_id: str, as_of: str | None = None, symbol: str | None = None):
            return runtime.research.features(dataset_id, as_of=as_of, symbol=symbol)

        @app.post("/api/research/experiments", status_code=202, response_model=JobResponse)
        def research_experiment(body: dict):
            return runtime.store.enqueue("research_experiment", body)

        @app.get("/api/research/experiments/{experiment_id}", response_model=dict)
        def experiment_result(experiment_id: str):
            return runtime.research.get_experiment(experiment_id)

        @app.post("/api/research/models/{model_id}/status", response_model=dict)
        def model_status(model_id: str, body: ModelStatusInput):
            return runtime.research.set_model_status(model_id, body.status)

        @app.get("/api/reports", response_model=ReportResponse)
        def reports():
            return runtime.reports()

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    def unavailable_api(path: str):
        raise HTTPException(404, "요청 경로가 없습니다.")

    distribution = Path(__file__).resolve().parents[4] / "frontend" / "dist"
    if (distribution / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=distribution / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def local_frontend(path: str):
            if path.startswith("api/") or path in {"api", "docs", "openapi.json"}:
                raise HTTPException(404, "요청 경로가 없습니다.")
            candidate = (distribution / path).resolve()
            if candidate.is_relative_to(distribution.resolve()) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(distribution / "index.html")

    return app
