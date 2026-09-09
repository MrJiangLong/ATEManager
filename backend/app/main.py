"""FastAPI 应用入口：装配路由、初始化 schema 与默认管理员、托管前端构建产物。

- `/api/v1/*`     通道一：上位机 pytest（X-API-Key）
- `/api/admin/*`  通道二：Web 管理（JWT）
- `/api/auth/*`   登录
- `/*`            前端 dist 静态资源（存在时自动启用，SPA 回退）
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import SessionLocal, engine, ensure_schema
from .errors import AppError
from .logging import configure_logging, get_logger
from .models import User
from .routers import (
    auth,
    clients,
    masters,
    metrics,
    products,
    records,
    repairs,
    routing,
    sessions,
    v1,
)
from .schemas import EnvelopeOut
from .security import hash_password
from .services import sweeper

configure_logging()
logger = get_logger("startup")


def _bootstrap() -> None:
    ensure_schema()
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            db.add(
                User(
                    username=settings.DEFAULT_ADMIN_USERNAME,
                    password_hash=hash_password(settings.DEFAULT_ADMIN_PASSWORD),
                    full_name=settings.DEFAULT_ADMIN_NAME,
                )
            )
            db.commit()
            logger.info("已创建默认管理员：%s", settings.DEFAULT_ADMIN_USERNAME)
    except Exception as exc:
        db.rollback()
        logger.exception("初始化管理员失败：%s", exc)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    _bootstrap()
    sweeper.start()
    try:
        yield
    finally:
        await sweeper.stop()
        engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="ATE 防呆测试管理系统：用例ID驱动防漏测与防跳工位",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    """业务异常统一响应包：HTTP 状态码为主，exit_code 为辅。"""
    body = EnvelopeOut(
        ok=False,
        exit_code=exc.exit_code,
        code=exc.code,
        message=exc.message,
        data=exc.data,
    )
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
    return JSONResponse(status_code=exc.status_code, content=body.model_dump(), headers=headers)


for _router in (
    auth.router,
    v1.router,
    masters.router,
    routing.router,
    products.router,
    records.router,
    repairs.router,
    clients.router,
    sessions.router,
    metrics.router,
):
    app.include_router(_router)


@app.get("/api/health", tags=["system"], summary="健康检查")
def health():
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION}


# ---------- 前端静态资源（可选）----------
def _find_dist() -> Optional[Path]:
    for candidate in (
        Path(os.getenv("FRONTEND_DIST", "")),
        Path(__file__).resolve().parent.parent.parent / "frontend" / "dist",
        Path("/app/frontend/dist"),
        Path("/app/dist"),
    ):
        if str(candidate) and (candidate / "index.html").is_file():
            return candidate
    return None


DIST_DIR = _find_dist()
if DIST_DIR is not None:
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        assert DIST_DIR is not None
        target = DIST_DIR / full_path
        if full_path and target.is_file():
            return FileResponse(target)
        return FileResponse(DIST_DIR / "index.html")

    logger.info("托管前端构建产物：%s", DIST_DIR)
