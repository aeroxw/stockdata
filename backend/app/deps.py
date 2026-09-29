"""FastAPI 依赖项：当前用户、APIKey 鉴权、限流。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import decode_token, hash_api_key
from app.config import settings
from app.db import get_db
from app.models import ApiKey, User
from app.plans import bump_usage, check_daily_quota
from app.points import settle_expired
from app.ratelimit import check_rate_limit, is_blacklisted

bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------- 会话用户
def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少认证凭据")

    payload = decode_token(cred.credentials)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效或已过期")

    user = db.scalar(select(User).where(User.id == int(payload["sub"])))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已禁用")
    return user


def get_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user


# ---------------------------------------------------------------- APIKey
#: API Key 允许的传递方式，三选一即可，方便不同语言的客户端接入
APIKEY_HEADER = "X-API-Key"
APIKEY_QUERY = "apikey"
HELP = (
    "缺少 API Key。三种方式任选其一："
    "Authorization: Bearer sk_live_xxx / X-API-Key: sk_live_xxx / ?apikey=sk_live_xxx"
)


def _extract_apikey(
    request: Request,
    cred: HTTPAuthorizationCredentials | None,
) -> str | None:
    # 1) Authorization: Bearer <key>
    if cred is not None and cred.credentials:
        return cred.credentials.strip()
    # 2) X-API-Key: <key>
    h = request.headers.get(APIKEY_HEADER)
    if h:
        return h.strip()
    # 3) ?apikey=<key>  （浏览器地址栏直接调试用）
    q = request.query_params.get(APIKEY_QUERY)
    if q:
        return q.strip()
    return None


def enforce_daily_quota(db: Session, user: User | None) -> None:
    """档位日调用配额。-1 = 不限。

    与「每分钟限流」互补：限流管突发，配额管总量。

    「有效期」由积分兑换产生：签到攒分换来的档位到期后回落到免费版，
    但**只降档、不停服**（见下方 settle_expired）。额度不牵扯任何金额。
    """
    if user is None:
        return

    #: 积分兑换来的档位到期 -> 回落到免费版，**继续服务**。
    #: 放在鉴权链里而不是另起一个定时任务：不需要额外 cron，
    #: 而且用户下一次调用就能立即用上新档位，不会出现
    #: "后台显示已过期、接口还在按高档位放行"的错位。
    #: 注意这里只降级、不拒绝 —— 配额可以变小，服务不能断。
    settle_expired(db, user)

    ok, used, quota = check_daily_quota(db, user)
    if not ok:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"今日调用配额已用尽（{used}/{quota}），请明日再试或联系管理员调整额度",
        )
    bump_usage(user.id)


def _load_apikey(request: Request, raw: str, db: Session) -> ApiKey:
    """校验 + 限流 + 计数，两处鉴权入口共用。"""
    if not raw.startswith("sk_live_"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Key 格式不正确")

    kh = hash_api_key(raw)
    if is_blacklisted(kh):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Key 已被吊销")

    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == kh))
    if not key or not key.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Key 无效")

    if key.expires_at and key.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Key 已过期")

    allowed, remain = check_rate_limit(key.id, key.rate_limit)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "调用频率超限",
            headers={"Retry-After": "60"},
        )

    # 记录本次上下文，供日志中间件使用
    request.state.apikey = key
    request.state.rate_remain = remain
    #: 关键：这里就把 user_id / key_id 取成**纯 Python int** 存下来。
    #: 日志中间件跑在路由函数之后，那时 get_db 的 session 已经关了，
    #: 再去读 key.user_id 会撞 DetachedInstanceError → 整个请求变 500。
    #: （线上真实踩到过：/special 用 API Key 调用偶发 500）
    request.state.user_id = int(key.user_id)
    request.state.key_id = int(key.id)

    # 档位日配额（按用户维度，多个 Key 共享同一份额度）
    enforce_daily_quota(db, db.scalar(select(User).where(User.id == key.user_id)))

    key.last_used = datetime.utcnow()
    key.total_calls = (key.total_calls or 0) + 1
    db.commit()
    return key


def authenticate_apikey(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> ApiKey:
    """对外数据接口的鉴权：Bearer sk_live_xxx / X-API-Key / ?apikey="""
    raw = _extract_apikey(request, cred)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, HELP)
    return _load_apikey(request, raw, db)


class Principal:
    """统一的调用主体：可能是 API Key，也可能是登录用户（控制台页面）。"""

    def __init__(self, apikey: ApiKey | None = None, user: User | None = None):
        self.apikey = apikey
        self.user = user

    @property
    def scopes(self) -> list[str]:
        if self.apikey:
            return self.apikey.scopes or []
        # 登录用户默认拥有全部数据权限
        return ["quote", "kline", "special"]

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def authenticate_any(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> Principal:
    """API Key 与登录态二选一。

    控制台页面用 JWT，第三方程序用 API Key，两条路都能走通。
    """
    raw = _extract_apikey(request, cred)

    # 1) 明确是 API Key（sk_live_ 前缀）走密钥分支
    if raw and raw.startswith("sk_live_"):
        return Principal(apikey=_load_apikey(request, raw, db))

    # 2) 用 X-API-Key / ?apikey= 显式传了值但格式不对 —— 直接报错，不能静默放行
    explicit = (
        request.headers.get(APIKEY_HEADER)
        or request.query_params.get(APIKEY_QUERY)
    )
    if explicit:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Key 格式不正确")

    # 3) 剩下的按登录态处理（Authorization: Bearer <JWT>）
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, HELP + "，或先登录")
    payload = decode_token(cred.credentials)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效或已过期")
    user = db.scalar(select(User).where(User.id == int(payload["sub"])))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已禁用")
    enforce_daily_quota(db, user)
    request.state.user = user
    request.state.user_id = int(user.id)   # 同上，避免中间件读已游离的 ORM 对象
    return Principal(user=user)
