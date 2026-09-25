from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from invest_lab.adapters.sqlite import VersionConflict
from invest_lab.application.service import Workspace
from invest_lab.bootstrap import build_workspace


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
    symbol: str = Field(pattern=r"^\d{6}$")
    quantity: int = Field(strict=True, ge=1, le=100000)
    request_id: UUID


def create_app(workspace: Workspace | None = None) -> FastAPI:
    workspace = workspace or build_workspace()
    app = FastAPI(title="invest-lab local API", version="0.1.0")
    origins = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ]
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
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "허용되지 않은 요청 출처입니다."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "mode": "local_simulation",
            "source": workspace.market.source,
            "broker_connected": False,
            "live_orders_enabled": False,
        }

    @app.get("/api/screener")
    def screener():
        return workspace.screen()

    @app.get("/api/instruments/{symbol}")
    def detail(symbol: str):
        try:
            return workspace.detail(symbol)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/portfolio")
    def portfolio():
        return workspace.account()

    @app.get("/api/strategies")
    def strategies():
        return {"versions": [asdict(s) for s in workspace.strategies.versions()]}

    @app.post("/api/strategies", status_code=201)
    def save_strategy(body: StrategyInput):
        try:
            return asdict(
                workspace.save_strategy(
                    body.model_dump(exclude={"expected_version"}), body.expected_version
                )
            )
        except VersionConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/paper/buys", status_code=201)
    def paper_buy(body: PaperBuyInput):
        try:
            return workspace.buy(body.symbol, body.quantity, str(body.request_id))
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    return app
