"""服务端业务模块。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import engine
from .models import Base
from .routers import router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # 无迁移框架的轻量部署：启动时确保全部表结构存在（幂等）。
    Base.metadata.create_all(engine)
    yield


app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot. "
        "Duplicate student identities are reconciled through reviewable, "
        "revocable merge cases versioned as alias mappings."
    ),
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
