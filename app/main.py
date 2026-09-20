"""直播字幕定稿服务（FastAPI 入口）。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Query, Request

from .db import Database
from .schemas import (
    ChannelStatus,
    FinalizedResponse,
    FragmentIn,
    SubmitResponse,
)

DEFAULT_DB_PATH = "/data/subtitles.db"


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 启动时自动建库建表（CREATE TABLE IF NOT EXISTS，幂等）
        app.state.db = Database(path)
        yield
        app.state.db.close()

    app = FastAPI(
        title="Live Subtitle Finalizer",
        version="1.0.0",
        description="直播字幕定稿服务：乱序片段接收、修订裁决、水位线推进与定稿查询",
        lifespan=lifespan,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/channels/{channel}/fragments",
        response_model=SubmitResponse,
        summary="提交字幕片段",
    )
    def submit_fragment(
        channel: str, fragment: FragmentIn, request: Request
    ) -> SubmitResponse:
        result = request.app.state.db.submit_fragment(
            channel, fragment.seq, fragment.revision, fragment.text
        )
        return SubmitResponse(**result)

    @app.get(
        "/channels/{channel}/finalized",
        response_model=FinalizedResponse,
        summary="查询频道已定稿字幕",
    )
    def get_finalized(
        channel: str,
        request: Request,
        from_seq: int = Query(1, ge=1, description="起始序号（含）"),
        limit: int | None = Query(None, ge=1, le=10000),
    ) -> FinalizedResponse:
        result = request.app.state.db.get_finalized(channel, from_seq, limit)
        return FinalizedResponse(**result)

    @app.get(
        "/channels/{channel}",
        response_model=ChannelStatus,
        summary="查询频道状态",
    )
    def get_status(channel: str, request: Request) -> ChannelStatus:
        return ChannelStatus(**request.app.state.db.get_status(channel))

    return app


app = create_app()
