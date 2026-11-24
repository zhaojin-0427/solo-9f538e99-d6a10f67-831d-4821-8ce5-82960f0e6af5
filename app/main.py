"""FastAPI 应用入口。

启动方式：
    uvicorn app.main:app --host 0.0.0.0 --port 8000

交互式文档位于 ``/docs``。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError

from .config import database_url_from_env
from .db import Database
from .errors import DomainError
from .routes import router
from .service import initialize

logger = logging.getLogger("student_status")


def create_app(database_url: str | None = None) -> FastAPI:
    url = database_url or database_url_from_env()
    database = Database(url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 只在启动时建表，导入模块不产生数据库文件。
        initialize(database)
        app.state.database = database
        logger.info("学籍更正清单服务已启动，数据库：%s", url)
        try:
            yield
        finally:
            database.dispose()

    app = FastAPI(
        title="学籍更正清单 API",
        description=(
            "按批次登记转学单，整批生效后按批号、学校、统计日查询补报与撤回名单。"
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.database = database

    @app.exception_handler(DomainError)
    async def _domain_error_handler(
        _request: Request, exc: DomainError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.to_payload()},
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error_handler(
        _request: Request, exc: IntegrityError
    ) -> JSONResponse:
        logger.warning("数据库约束冲突：%s", exc)
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "data_conflict",
                    "message": "数据约束冲突，请检查转学单号、学生与生效日是否重复",
                }
            },
        )

    @app.exception_handler(OperationalError)
    async def _operational_error_handler(
        _request: Request, exc: OperationalError
    ) -> JSONResponse:
        logger.exception("数据库忙或不可用：%s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "database_busy",
                    "message": "数据库繁忙，请稍后重试",
                }
            },
        )

    app.include_router(router)
    return app


app = create_app()
