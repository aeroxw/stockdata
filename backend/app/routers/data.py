"""对外数据接口（需 API Key）。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adaptors import KLINE_CHAIN, SNAPSHOT_CHAIN, build_adaptors
from app.adaptors.base import AdaptorError, normalize_code
from app.config import settings
from app.db import get_db
from app.deps import Principal, authenticate_any
from app.models import StockMeta
from app.ratelimit import cache_get, cache_set
from app.schemas import ApiResponse, Meta

router = APIRouter(prefix="/api/v1", tags=["数据接口"])

# 适配器单例（进程内复用连接池）
_SNAPSHOT_ADAPTORS = build_adaptors(SNAPSHOT_CHAIN)
_KLINE_ADAPTORS = build_adaptors(KLINE_CHAIN)
_SPECIAL = build_adaptors(["hithink", "kaipanla"])


def _agg(adaptors):
    from app.adaptors.base import QuoteAggregator

    return QuoteAggregator(adaptors)


def _fill_name(db: Session, items: list[dict]) -> None:
    """同花顺快照不返回中文名，用本地映射表补全。"""
    miss = [i["code"] for i in items if not i.get("name")]
    if not miss:
        return
    rows = db.query(StockMeta).filter(StockMeta.code.in_(miss)).all()
    m = {r.code: r.name for r in rows}
    for i in items:
        if not i.get("name"):
            i["name"] = m.get(i["code"], "")


# ---------------------------------------------------------------- 公开演示接口
#: 主页展示用的热门标的，免鉴权（只读、固定标的，不会造成数据源压力）
DEMO_CODES = ["000001.SH", "600519.SH", "000858.SZ", "601318.SH", "600036.SH", "000001.SZ"]


@router.get("/public/quote", summary="公开行情（主页展示，免鉴权）")
async def public_quote(db: Session = Depends(get_db)):
    ck = "pub:quote"
    cached = cache_get(ck)
    if cached:
        return ApiResponse(data=cached["data"], meta=Meta(source=cached["src"], cached=True))

    try:
        quotes, src = await _agg(_SNAPSHOT_ADAPTORS).get_quotes(DEMO_CODES)
    except AdaptorError as exc:
        raise HTTPException(502, f"数据源暂不可用: {exc}") from exc

    items = [json.loads(q.model_dump_json()) for q in quotes]
    _fill_name(db, items)
    cache_set(ck, {"data": items, "src": src}, 10)
    return ApiResponse(data=items, meta=Meta(source=src, ts=datetime.now()))


# ---------------------------------------------------------------- 市场概览
#: 首页大盘概览用的三大指数 + 科创50
INDEX_CODES = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH"]
INDEX_NAMES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
}


async def _fetch_indices() -> list[dict]:
    """指数走腾讯（同花顺快照里的指数口径不一致）。"""
    try:
        quotes, _ = await _agg(_SNAPSHOT_ADAPTORS).get_quotes(INDEX_CODES)
    except Exception:  # noqa: BLE001  概览降级不能拖垮整页
        return []
    out = []
    for q in quotes:
        d = json.loads(q.model_dump_json())
        d["name"] = INDEX_NAMES.get(q.code, q.name or q.code)
        out.append(d)
    return out


@router.get("/public/market", summary="市场概览（主页展示，免鉴权）")
async def public_market():
    """一次返回：三大指数 + 全市场涨跌家数 + 领涨领跌榜。

    涨跌统计走同花顺「一次请求回全市场 5578 只」的能力，成本恒定。
    """
    ck = "pub:market"
    cached = cache_get(ck)
    if cached:
        return ApiResponse(data=cached["data"], meta=Meta(source=cached["src"], cached=True))

    indices, breadth, movers = await asyncio.gather(
        _fetch_indices(), _market_breadth(), _top_movers(), return_exceptions=True
    )
    data = {
        "indices": indices if isinstance(indices, list) else [],
        "breadth": breadth if isinstance(breadth, dict) else {},
        "top_gainers": movers[0] if isinstance(movers, tuple) else [],
        "top_losers": movers[1] if isinstance(movers, tuple) else [],
    }
    cache_set(ck, {"data": data, "src": "hithink+tencent"}, 30)
    return ApiResponse(data=data, meta=Meta(source="hithink+tencent", ts=datetime.now()))


async def _market_breadth() -> dict:
    """全市场涨跌家数统计。"""
    quotes = await _SPECIAL[0].fetch_all_quotes()
    up = down = flat = limit_up = limit_down = 0
    total = 0
    pct_sum = 0.0
    pcts: list[float] = []
    for q in quotes:
        p = q.change_pct or 0.0
        if q.price is None or q.price <= 0:
            continue
        total += 1
        pct_sum += p
        pcts.append(p)
        if p > 0:
            up += 1
        elif p < 0:
            down += 1
        else:
            flat += 1
        # 近似判断涨跌停：主板 ±10%，创业板/科创板 ±20%，这里用宽松阈值 + 单独看 20% 档
        if p >= 9.8:
            limit_up += 1
        elif p <= -9.8:
            limit_down += 1
    pcts.sort()
    median = pcts[len(pcts) // 2] if pcts else 0.0
    return {
        "total": total,
        "up": up,
        "down": down,
        "flat": flat,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "avg_pct": round(pct_sum / total, 3) if total else 0.0,
        "median_pct": round(median, 3),
        "up_ratio": round(up / total * 100, 1) if total else 0.0,
    }


async def _top_movers(n: int = 6) -> tuple[list[dict], list[dict]]:
    """领涨 / 领跌榜。同花顺快照不带名称，用腾讯补一次（一次批量请求搞定）。"""
    quotes = await _SPECIAL[0].fetch_all_quotes()
    pool = [q for q in quotes if (q.price or 0) > 0 and q.change_pct is not None]
    pool.sort(key=lambda q: q.change_pct)
    losers = pool[:n]
    gainers = pool[-n:][::-1]

    # 用腾讯批量补名称（12 只一次请求）
    codes = [q.code for q in gainers + losers]
    names: dict[str, str] = {}
    try:
        tq, _ = await _agg(_SNAPSHOT_ADAPTORS).get_quotes(codes)
        names = {q.code: q.name for q in tq if q.name}
    except Exception:  # noqa: BLE001
        pass

    def pack(qs):
        return [
            {
                "code": q.code,
                "name": names.get(q.code, ""),
                "price": q.price,
                "change_pct": round(q.change_pct or 0.0, 2),
            }
            for q in qs
        ]

    return pack(gainers), pack(losers)


# ---------------------------------------------------------------- 行情快照
@router.get("/quote", summary="实时行情快照")
async def quote(
    request: Request,
    codes: str = Query(..., description="代码，逗号分隔，如 600000.SH,000001.SZ"),
    source: str | None = Query(None, description="指定数据源，默认自动选主源"),
    db: Session = Depends(get_db),
    who: Principal = Depends(authenticate_any),
):
    if not who.has("quote"):
        raise HTTPException(403, "该 API Key 无 quote 权限")

    code_list = [normalize_code(c) for c in codes.split(",") if c.strip()]
    if not code_list:
        raise HTTPException(400, "codes 不能为空")
    if len(code_list) > 200:
        raise HTTPException(400, "单次最多查询 200 只")

    ck = f"q:{','.join(sorted(code_list))}:{source or 'auto'}"
    cached = cache_get(ck)
    if cached:
        return ApiResponse(data=cached["data"], meta=Meta(source=cached["src"], cached=True))

    try:
        quotes, src = await _agg(_SNAPSHOT_ADAPTORS).get_quotes(
            code_list, [source] if source else None
        )
    except AdaptorError as exc:
        raise HTTPException(502, f"数据源不可用: {exc}") from exc

    items = [json.loads(q.model_dump_json()) for q in quotes]
    _fill_name(db, items)

    cache_set(ck, {"data": items, "src": src}, settings.QUOTE_CACHE_TTL)
    return ApiResponse(data=items, meta=Meta(source=src, cached=False, ts=datetime.now()))


# ---------------------------------------------------------------- K 线
@router.get("/kline", summary="历史 K 线")
async def kline(
    code: str = Query(..., description="如 600519.SH"),
    period: str = Query("day", pattern="^(day|week|month|60min|30min|15min|5min|1min)$"),
    limit: int = Query(100, ge=1, le=2000),
    adjust: str = Query("qfq", pattern="^(none|qfq|hfq)$"),
    source: str | None = Query(None),
    who: Principal = Depends(authenticate_any),
):
    if not who.has("kline"):
        raise HTTPException(403, "该 API Key 无 kline 权限")

    code = normalize_code(code)
    ck = f"k:{code}:{period}:{limit}:{adjust}:{source or 'auto'}"
    cached = cache_get(ck)
    if cached:
        return ApiResponse(data=cached["data"], meta=Meta(source=cached["src"], cached=True))

    adaptors = _KLINE_ADAPTORS
    if source:
        adaptors = build_adaptors([source])

    last_err: Exception | None = None
    for ad in adaptors:
        try:
            bars = await ad.fetch_kline(code, period, limit, adjust)
            if bars:
                data = [json.loads(b.model_dump_json()) for b in bars]
                cache_set(ck, {"data": data, "src": ad.name}, settings.KLINE_CACHE_TTL)
                return ApiResponse(data=data, meta=Meta(source=ad.name))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue

    raise HTTPException(502, f"K 线获取失败: {last_err}")


# ---------------------------------------------------------------- 特色数据
_SPECIAL_KINDS = {
    "limit_up": "涨停池",
    "limit_down": "跌停池",
    "limit_break": "炸板池",
    "limit_up_ladder": "连板天梯",
    "dragon_tiger": "龙虎榜",
    "skyrocket": "飙升榜",
    "hot_stock": "热股榜",
    "anomaly": "异动分析",
}


@router.get("/special/{kind}", summary="特色数据（涨停池/龙虎榜等）")
async def special(
    kind: str,
    source: str | None = Query(None, description="hithink(默认) 或 kaipanla(仅龙虎榜)"),
    who: Principal = Depends(authenticate_any),
):
    if kind not in _SPECIAL_KINDS:
        raise HTTPException(400, f"不支持的类型，可选: {', '.join(_SPECIAL_KINDS)}")
    if not who.has("special"):
        raise HTTPException(403, "该 API Key 无 special 权限")

    if source == "kaipanla":
        if kind != "dragon_tiger":
            raise HTTPException(400, "开盘了目前仅支持 dragon_tiger（龙虎榜）")
        try:
            data = await _SPECIAL[1].dragon_tiger()
            return ApiResponse(data=data, meta=Meta(source="kaipanla"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"开盘了数据源不可用: {exc}") from exc

    try:
        data = await _SPECIAL[0].special(kind)
        return ApiResponse(data=data, meta=Meta(source="hithink"))
    except AdaptorError as exc:
        # 龙虎榜可降级到开盘了
        if kind == "dragon_tiger":
            try:
                return ApiResponse(
                    data=await _SPECIAL[1].dragon_tiger(),
                    meta=Meta(source="kaipanla"),
                )
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(502, f"特色数据获取失败: {exc}") from exc


@router.get("/special", summary="特色数据类型清单")
def special_kinds(_who: Principal = Depends(authenticate_any)):
    return ApiResponse(
        data=[{"kind": k, "desc": v} for k, v in _SPECIAL_KINDS.items()]
    )


# ---------------------------------------------------------------- 元数据
@router.get("/stock/list", summary="股票列表")
def stock_list(
    q: str | None = Query(None, description="按代码或名称模糊搜索"),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    _who: Principal = Depends(authenticate_any),
):
    stmt = db.query(StockMeta)
    if q:
        like = f"%{q}%"
        stmt = stmt.filter(StockMeta.code.like(like) | StockMeta.name.like(like))
    rows = stmt.limit(limit).all()
    return ApiResponse(
        data=[{"code": r.code, "name": r.name, "exchange": r.exchange} for r in rows],
        meta=Meta(source="db"),
    )


@router.get("/sources", summary="数据源健康状态")
def sources(_who: Principal = Depends(authenticate_any)):
    out = []
    seen: set[str] = set()
    for ad in _SNAPSHOT_ADAPTORS + _KLINE_ADAPTORS + _SPECIAL:
        if ad.name in seen:
            continue
        seen.add(ad.name)
        out.append({"source": ad.name, **ad.stats})
    return ApiResponse(data=out)
