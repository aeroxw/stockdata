"""开盘了适配器 —— 龙虎榜 / 情绪数据补充源。

走 NAS 上已部署的 kpl-proxy：http://<NAS内网IP>:8018
该代理已解决签名问题：自动注入 DeviceID / PhoneOSNew=2 / VerSion=5.23.0.1 / apiv=w44，
并按 c 参数路由到 apphis(历史) / apphwhq(今日) / applhb(龙虎榜) 三个上游。

已验证可用：
  GET /w1/api/index.php?c=LongHuBang&a=GetStockList
  → 55 条龙虎榜，字段含 BuyIn(净买入) / JoinNum(席位数) / CircPrice(流通市值) / TurnoverRatio

注意：c=HomeDingPan 系列实测返回空，可能需要代理配置 KPL_TOKEN 环境变量。
"""

from __future__ import annotations

from typing import Any, Sequence

from app.adaptors.base import AdaptorError, BaseAdaptor, normalize_code, safe_float
from app.config import settings


class KaipanlaAdaptor(BaseAdaptor):
    name = "kaipanla"
    support_batch = False
    batch_size = 1
    concurrency = 2

    def __init__(self) -> None:
        super().__init__()
        self.base = settings.KPL_PROXY_BASE.rstrip("/")

    async def _call(self, params: dict) -> Any:
        data = await self._get_json(f"{self.base}/w1/api/index.php", params)
        if not isinstance(data, dict):
            raise AdaptorError("[kaipanla] 上游返回非 JSON（可能参数有误或需 Token）")
        err = data.get("errcode")
        if err not in (None, "", "0", 0):
            raise AdaptorError(f"[kaipanla] errcode={err} {data.get('errmsg') or ''}")
        return data

    async def fetch_quotes(self, codes: Sequence[str]) -> list[Quote]:
        """开盘了不提供通用快照，这里返回空；请用 dragon_tiger 等特色方法。"""
        return []

    # ---------------------------------------------------------------- 特色数据
    async def dragon_tiger(self) -> dict:
        """龙虎榜列表（已实测可用）。"""
        data = await self._call({"c": "LongHuBang", "a": "GetStockList"})
        items = data.get("list") or []
        norm = []
        for it in items:
            norm.append({
                "code": normalize_code(str(it.get("ID") or "")),
                "name": it.get("Name") or "",
                "change_pct": safe_float((it.get("IncreaseAmount") or "0").rstrip("%")),
                "net_buy": safe_float(it.get("BuyIn")),          # 净买入（元）
                "seat_count": it.get("JoinNum"),
                "turnover": safe_float(it.get("Turnover")),      # 成交额
                "circ_mv": safe_float(it.get("CircPrice")),      # 流通市值
                "amplitude": safe_float(it.get("Amplitude")),
                "turnover_rate": safe_float(it.get("TurnoverRatio")),
                "capitalization": safe_float(it.get("Capitalization")),
            })
        return {
            "source": self.name,
            "date": data.get("Time"),
            "count": len(norm),
            "items": norm,
        }

    async def raw(self, c: str, a: str, **extra: Any) -> dict:
        """通用透传，便于后续扩展盯盘/涨停复盘/游资席位等接口。"""
        params = {"c": c, "a": a}
        params.update(extra)
        return await self._call(params)
