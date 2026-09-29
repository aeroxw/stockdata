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
    HITHINK_API_KEY: str = ""
    HITHINK_BASE: str = "https://fuyao.aicubes.cn"
    EASYTDX_BASE: str = "http://NAS_INTERNAL_IP:8000"
    KPL_PROXY_BASE: str = "http://NAS_INTERNAL_IP:8018"

    # ---- 限流默认值 ----
    DEFAULT_RATE_LIMIT: int = 60  # 次/分钟

    #: 是否允许「模拟支付」通道（点一下立刻到账，不经过任何真实收款）。
    #:
    #: 本地开发 / 自动化测试必须为 True，否则 test_billing 整条充值链路跑不了。
    #: **部署到公网 / NAS 对外时必须设为 False** —— 否则任何注册用户都能
    #: 给自己无限充值，再拿余额买下 VIP 配额，等于白送。
    #:
    #: 关掉后用户只能用 manual（线下转账，等后台确认）或真实渠道，
    #: 站点照样能完整跑通，只是收不到钱而已。
    ALLOW_MOCK_PAY: bool = True

    # ---- 新用户试用期 ----
    #: 注册即送的体验期天数。0 或 False 表示不给试用期（注册直接是免费版）。
    TRIAL_ENABLED: bool = True
    TRIAL_DAYS: int = 15
    #: 试用期内享受哪个套餐的配额。留空则回落到 free。
    TRIAL_PLAN: str = "pro"
    #: 试用到期后是否直接停止 API 调用（402）。
    #: False = 到期只提示不拦截，用户仍能继续调用。
    TRIAL_EXPIRE_BLOCK: bool = True

    # ---- 站点 ----
    #: 对外访问地址，用于拼接支付回调 URL。形如 https://stockdata.example.com
    #: 回调地址必须公网可达，微信/支付宝收不到回调就永远不会入账。
    SITE_BASE_URL: str = ""

    # ---- 支付：微信支付（Native 扫码，API v3）----
    #: 留空即视为"该渠道未接入"，前端不会展示入口，下单会返 503
    WXPAY_APPID: str = ""
    WXPAY_MCHID: str = ""
    #: 商户 API 证书序列号（在微信商户平台 -> API 证书 里看）
    WXPAY_SERIAL_NO: str = ""
    #: APIv3 密钥（32 字节），用于解密回调报文里的 resource
    WXPAY_API_V3_KEY: str = ""
    #: 商户私钥：可以直接放 PEM 全文，也可以放**私钥文件路径**（推荐后者）
    WXPAY_PRIVATE_KEY: str = ""
    #: 微信支付平台公钥（v3 新方式）或平台证书公钥，用于校验回调签名
    WXPAY_PLATFORM_PUBKEY: str = ""
    WXPAY_NOTIFY_URL: str = ""

    # ---- 支付：支付宝（当面付 alipay.trade.precreate）----
    ALIPAY_APPID: str = ""
    #: 应用私钥（PKCS#1 或 PKCS#8 均可），同样支持 PEM 全文或文件路径
    ALIPAY_PRIVATE_KEY: str = ""
    #: 支付宝公钥，用于校验回调/返回签名
    ALIPAY_PUBLIC_KEY: str = ""
    ALIPAY_NOTIFY_URL: str = ""
    ALIPAY_RETURN_URL: str = ""

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
