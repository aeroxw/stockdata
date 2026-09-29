"""数据源适配层。"""

from app.adaptors.base import BaseAdaptor, QuoteAggregator
from app.adaptors.eastmoney import EastmoneyAdaptor
from app.adaptors.hithink import HithinkAdaptor
from app.adaptors.kaipanla import KaipanlaAdaptor
from app.adaptors.sina import SinaAdaptor
from app.adaptors.tdx import TdxAdaptor
from app.adaptors.tencent import TencentAdaptor

__all__ = [
    "BaseAdaptor",
    "QuoteAggregator",
    "HithinkAdaptor",
    "TencentAdaptor",
    "SinaAdaptor",
    "EastmoneyAdaptor",
    "TdxAdaptor",
    "KaipanlaAdaptor",
]

#: 快照源优先级：腾讯（盘中高频、限流友好）→ 同花顺（全市场一次拿）→ 新浪 → 东财
SNAPSHOT_CHAIN = ["tencent", "hithink", "sina", "eastmoney"]

#: K 线源优先级：easy-tdx（批量快）→ 同花顺 → 东财
KLINE_CHAIN = ["tdx", "hithink", "eastmoney"]


def build_adaptors(names: list[str] | None = None) -> list[BaseAdaptor]:
    """按名字构造适配器。names 为空则返回全部。"""
    pool: dict[str, type[BaseAdaptor]] = {
        "hithink": HithinkAdaptor,
        "tencent": TencentAdaptor,
        "sina": SinaAdaptor,
        "eastmoney": EastmoneyAdaptor,
        "tdx": TdxAdaptor,
        "kaipanla": KaipanlaAdaptor,
    }
    if names:
        return [pool[n]() for n in names if n in pool]
    return [cls() for cls in pool.values()]
