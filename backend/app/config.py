"""全局配置。所有敏感值从环境变量或 .env 读取，禁止硬编码。"""

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: backend/app/config.py -> backend/
_BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # 显式指向 backend/.env，这样采集器从项目根目录启动时也能读到同一份配置
        env_file=(_BACKEND_DIR / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 应用 ----
    APP_NAME: str = "StockData 金融数据平台"
    DEBUG: bool = False

    # ---- 数据库 ----
    # 生产: postgresql+psycopg://user:pwd@postgres:5432/stockdata
    # 本地默认用 backend/stockdata.db（绝对路径，避免采集器与 API 各写一份）
    DATABASE_URL: str = f"sqlite:///{(_BACKEND_DIR / 'stockdata.db').as_posix()}"

    # ---- Redis ----
    REDIS_URL: str = "redis://localhost:6379/0"

    # ---- 安全 ----
    JWT_SECRET: str = "change-me-in-production-please-use-a-long-random-string"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 120
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ---- 数据源 ----
    #: 六个适配器里有三家走"外部 HTTP 服务"，另外三家直连公开接口。
    #: 前两个地址指的是你自己搭的服务（不在本仓库范围内）：
    #:   EASYTDX_BASE   —— easy-tdx（通达信协议转 HTTP）
    #:   KPL_PROXY_BASE —— 开盘了数据代理
    #: 没搭就不填，对应数据源会自动降级，其它五家照常工作。
    HITHINK_API_KEY: str = ""
    HITHINK_BASE: str = "https://fuyao.aicubes.cn"
    EASYTDX_BASE: str = "http://127.0.0.1:8000"
    KPL_PROXY_BASE: str = "http://127.0.0.1:8018"

    # ---- 限流默认值 ----
    DEFAULT_RATE_LIMIT: int = 60  # 次/分钟

    # ---- 缓存 ----
    QUOTE_CACHE_TTL: int = 3  # 秒，盘中快照缓存
    KLINE_CACHE_TTL: int = 300  # 秒

    @model_validator(mode="after")
    def _absolutize_sqlite(self) -> "Settings":
        """把相对路径的 SQLite 锚定到 backend/。

        否则 API 服务（cwd=backend）和采集器（cwd=项目根）会各写一份库文件，
        表现为"采集成功但接口查不到"。
        """
        url = self.DATABASE_URL
        if url.startswith("sqlite") and "///" in url:
            path = url.split("///", 1)[1]
            if path and not path.startswith(":memory:") and not Path(path).is_absolute():
                self.DATABASE_URL = f"sqlite:///{(_BACKEND_DIR / path).as_posix()}"
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
