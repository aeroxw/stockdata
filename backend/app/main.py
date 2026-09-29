"""StockData 金融数据平台 —— FastAPI 入口。

同时托管前端静态页面（/），因此部署时只需要一个 api 容器即可对外提供
「网站 + API」的完整能力。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import init_db
from app.models import ApiLog
from app.routers import admin, apikey, auth, billing, data, pay
from app.db import SessionLocal

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stockdata")

app = FastAPI(
    title=settings.APP_NAME,
    description="多源 A 股金融数据整合平台：通达信 / 同花顺 / 开盘了 / 东方财富 / 新浪 / 腾讯",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- 调用日志
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    latency = int((time.perf_counter() - start) * 1000)

    # 只记录 API 调用，静态页面不入日志
    if request.url.path.startswith("/api/v1/"):
        #: 只认**纯 int** 的 user_id / key_id（由 deps 在鉴权时存好）。
        #:
        #: 不能在这里访问 key.user_id / key.id：中间件跑在路由函数之后，
        #: get_db 的 session 已经关闭，ORM 对象处于 detached 状态，
        #: 读属性会抛 DetachedInstanceError。而且这行原本在 try 块**外面**，
        #: 异常直接冒泡成 500 —— 线上表现是"用 API Key 调接口偶发 500"，
        #: 跟业务代码毫无关系，极难排查。
        user_id = getattr(request.state, "user_id", None)
        key_id = getattr(request.state, "key_id", None)
        try:
            db = SessionLocal()
            db.add(ApiLog(
                key_id=key_id,
                user_id=user_id,
                path=request.url.path,
                status=response.status_code,
                latency_ms=latency,
            ))
            db.commit()
            db.close()
        except Exception:  # noqa: BLE001  日志失败不能影响主流程
            pass
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("未处理异常: %s", exc)
    # 调试模式下把真实原因透出，省得每次都去翻容器日志
    msg = f"{type(exc).__name__}: {exc}" if settings.DEBUG else "服务器内部错误"
    return JSONResponse(status_code=500, content={"code": 500, "msg": msg})


# ---------------------------------------------------------------- 路由
# 统一挂在 /api/v1 下，前端与第三方只需记一个基址
API = "/api/v1"
app.include_router(auth.router, prefix=API)
app.include_router(apikey.router, prefix=API)
app.include_router(data.router)
app.include_router(billing.router, prefix=API)
app.include_router(admin.router, prefix=API)
#: 支付回调。注意前缀同样是 /api/v1 —— 回调地址要在商户平台配置成
#: https://<SITE_BASE_URL>/api/v1/pay/notify/wechat
app.include_router(pay.router, prefix=API)


@app.get("/health")
def health():
    from app.ratelimit import is_available

    return {"ok": True, "redis": is_available()}


# ---------------------------------------------------------------- 静态前端
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/{page}.html", include_in_schema=False)
    def page(page: str):
        f = STATIC_DIR / f"{page}.html"
        if f.exists():
            return FileResponse(f)
        return FileResponse(STATIC_DIR / "index.html")


@app.on_event("startup")
def on_startup():
    init_db()
    log.info("StockData 启动完成，docs: /api/docs")
