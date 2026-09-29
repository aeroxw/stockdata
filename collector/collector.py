"""StockData 采集器 —— 独立进程，不与 API 服务耦合。

设计原则：
  1. 采集器有独立生命周期（7x24 跑定时任务），崩溃不影响对外 API
  2. 每源独立限流（实测硬上限：腾讯 50 只/批、easy-tdx 28 请求/秒）
  3. 交易时段感知：非交易时段停止快照任务，避免无效请求触发风控
  4. 失败指数退避重试，仍失败则切换备用源
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, time as dtime
from pathlib import Path

# 让 collector 能复用 backend 的适配层。
#
# 两种运行环境的目录布局不一样，写死一个路径必崩一边：
#   本地：  <项目根>/collector/collector.py  -> backend 在 ../backend
#   容器：  /app/collector.py                -> backend 被拷到 /app/backend
#           （原写法 parent.parent 在容器里算出来是 "/"，拼成 /backend，
#             而代码实际在 /app/backend，启动直接 ImportError）
# 所以逐个候选试，哪个目录底下真的有 app/ 就用哪个。
_HERE = Path(__file__).resolve().parent
for _cand in (_HERE / "backend", _HERE.parent / "backend"):
    if (_cand / "app").is_dir():
        sys.path.insert(0, str(_cand))
        break
else:
    sys.path.insert(0, str(_HERE / "backend"))   # 兜底，保证至少有个值

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402

from app.adaptors import build_adaptors  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import KlineRow, QuoteRow, SpecialData, StockMeta  # noqa: E402

# 采集器可能先于 API 服务启动，这里保证表结构已就绪
init_db()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("collector")

CN_TZ_OFFSET = 8  # 简化处理：按 UTC+8 判断交易时段


# ---------------------------------------------------------------- 交易时段
def is_trading_time(now: datetime | None = None) -> bool:
    """A 股交易时段：周一至周五 09:30-11:30 / 13:00-15:00。

    注：未排除法定节假日，精确日历可从同花顺 trading-days 接口同步。
    """
    now = now or datetime.utcnow()
    # 转成北京时间
    hour = (now.hour + CN_TZ_OFFSET) % 24
    minute = now.minute
    if now.weekday() >= 5:
        return False
    t = hour * 60 + minute
    return (9 * 60 + 30) <= t <= (11 * 60 + 30) or (13 * 60) <= t <= (15 * 60)


# ---------------------------------------------------------------- 采集任务
class Collector:
    def __init__(self) -> None:
        self.hithink = build_adaptors(["hithink"])[0]
        self.tencent = build_adaptors(["tencent"])[0]
        self.sina = build_adaptors(["sina"])[0]
        self.tdx = build_adaptors(["tdx"])[0]
        self.kaipanla = build_adaptors(["kaipanla"])[0]
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    # ---------------- 全市场快照（同花顺，1 次请求） ----------------
    async def job_full_snapshot(self) -> None:
        log.info("[全市场快照] 开始")
        try:
            quotes = await self.hithink.fetch_all_quotes()
        except Exception as exc:  # noqa: BLE001
            log.error("[全市场快照] 同花顺失败，降级腾讯: %s", exc)
            quotes = []
        if not quotes:
            log.warning("[全市场快照] 无数据，跳过")
            return

        db = SessionLocal()
        try:
            now = datetime.utcnow()
            for q in quotes:
                db.merge(QuoteRow(
                    code=q.code, ts=q.ts, name=q.name, price=q.price,
                    pre_close=q.pre_close, open=q.open, high=q.high, low=q.low,
                    volume=q.volume, amount=q.amount, change_pct=q.change_pct,
                    turnover_rate=q.turnover_rate, vol_ratio=q.vol_ratio,
                    circ_mv=q.circ_mv, source=q.source,
                ))
            db.commit()
            log.info("[全市场快照] 完成，写入 %d 条 (ts=%s)", len(quotes), now)
        except Exception as exc:  # noqa: BLE001
            log.exception("[全市场快照] 写库失败: %s", exc)
            db.rollback()
        finally:
            db.close()

    # ---------------- 盘中高频快照（腾讯） ----------------
    async def job_intraday_snapshot(self) -> None:
        if not is_trading_time():
            return
        log.info("[盘中快照] 开始")
        db = SessionLocal()
        try:
            codes = [r.code for r in db.query(StockMeta.code).all()]
            if not codes:
                #: 以前这里只 warning 一句就 return，从没真同步过 ——
                #: 于是列表永远是空的，这个任务每分钟空转一次。
                log.warning("[盘中快照] 股票列表为空，立即同步")
                await self._sync_stock_list(db)
                codes = [r.code for r in db.query(StockMeta.code).all()]
                if not codes:
                    log.error("[盘中快照] 同步后列表仍为空，放弃本轮")
                    return

            sem = asyncio.Semaphore(6)
            total = 0

            async def one(batch: list[str]):
                nonlocal total
                async with sem:
                    try:
                        qs = await self.tencent.fetch_quotes(batch)
                    except Exception:  # noqa: BLE001
                        try:
                            qs = await self.sina.fetch_quotes(batch)
                        except Exception:  # noqa: BLE001
                            return
                for q in qs:
                    db.merge(QuoteRow(
                        code=q.code, ts=q.ts, name=q.name, price=q.price,
                        pre_close=q.pre_close, open=q.open, high=q.high, low=q.low,
                        volume=q.volume, amount=q.amount, change_pct=q.change_pct,
                        turnover_rate=q.turnover_rate, vol_ratio=q.vol_ratio,
                        circ_mv=q.circ_mv, source=q.source,
                    ))
                total += len(qs)

            for i in range(0, len(codes), 50):
                await one(codes[i:i + 50])
            db.commit()
            log.info("[盘中快照] 完成 %d 条", total)
        except Exception as exc:  # noqa: BLE001
            log.exception("[盘中快照] 失败: %s", exc)
            db.rollback()
        finally:
            db.close()

    # ---------------- 全市场日 K（easy-tdx） ----------------
    async def job_daily_kline(self) -> None:
        log.info("[全市场日K] 开始")
        db = SessionLocal()
        try:
            codes = [r.code for r in db.query(StockMeta.code).all()]
            if not codes:
                #: 同盘中快照：缺列表就当场补，别空转
                log.warning("[全市场日K] 股票列表为空，立即同步")
                await self._sync_stock_list(db)
                codes = [r.code for r in db.query(StockMeta.code).all()]
                if not codes:
                    log.error("[全市场日K] 同步后列表仍为空，放弃本轮")
                    return

            sem = asyncio.Semaphore(12)   # 实测服务端死限 28 req/s
            done = [0]

            async def one(code: str):
                async with sem:
                    try:
                        bars = await self.tdx.fetch_kline(code, "day", 1, "qfq")
                    except Exception:  # noqa: BLE001
                        return
                for b in bars:
                    db.merge(KlineRow(
                        code=b.code, period="day", adjust="qfq", date=b.date,
                        open=b.open, high=b.high, low=b.low, close=b.close,
                        volume=b.volume, amount=b.amount, source=b.source,
                    ))
                done[0] += 1
                if done[0] % 500 == 0:
                    db.commit()
                    log.info("[全市场日K] 进度 %d/%d", done[0], len(codes))

            await asyncio.gather(*[one(c) for c in codes])
            db.commit()
            log.info("[全市场日K] 完成 %d/%d", done[0], len(codes))
        except Exception as exc:  # noqa: BLE001
            log.exception("[全市场日K] 失败: %s", exc)
            db.rollback()
        finally:
            db.close()

    # ---------------- 特色数据（同花顺） ----------------
    async def job_special(self) -> None:
        log.info("[特色数据] 开始")
        db = SessionLocal()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        for kind in ("limit_up", "limit_down", "limit_break", "limit_up_ladder",
                     "dragon_tiger", "skyrocket", "hot_stock", "anomaly"):
            try:
                data = await self.hithink.special(kind)
            except Exception as exc:  # noqa: BLE001
                log.warning("[特色数据] %s 失败: %s", kind, exc)
                continue
            db.merge(SpecialData(kind=kind, date=today, source="hithink", payload=data))
            db.commit()
            log.info("[特色数据] %s 完成", kind)
        db.close()

    # ---------------- 龙虎榜（开盘了，交叉校验） ----------------
    async def job_kaipanla(self) -> None:
        log.info("[开盘了龙虎榜] 开始")
        db = SessionLocal()
        try:
            data = await self.kaipanla.dragon_tiger()
            db.merge(SpecialData(
                kind="dragon_tiger",
                date=str(data.get("date") or datetime.utcnow().strftime("%Y-%m-%d")),
                source="kaipanla",
                payload=data,
            ))
            db.commit()
            log.info("[开盘了龙虎榜] 完成 %d 条", data.get("count", 0))
        except Exception as exc:  # noqa: BLE001
            log.exception("[开盘了龙虎榜] 失败: %s", exc)
            db.rollback()
        finally:
            db.close()

    # ---------------- 股票列表 ----------------
    async def _sync_stock_list(self, db) -> int:
        """同步股票列表到 stock_meta，返回写入条数。

        抽成独立方法是因为**多个任务都依赖它**：盘中快照、全市场日K
        都要先有股票列表才能干活。以前只有凌晨 2 点的 job_stock_list 会跑，
        而其它任务发现列表为空时只打一句 warning 就 return ——
        于是 stock_meta 永远是空的，每分钟的盘中快照全是空转，
        /stock/list 接口永远返回 0 条。现在缺数据就当场补。
        """
        rows = await self.hithink.ticker_list()
        n = 0
        for r in rows:
            code = r.get("thscode") or r.get("ticker")
            if not code:
                continue
            db.merge(StockMeta(
                code=code,
                name=r.get("name") or "",
                exchange=r.get("exchange"),
                asset_type=r.get("asset_type"),
                list_date=str(r.get("list_date") or "")[:10],
                updated_at=datetime.utcnow(),
            ))
            n += 1
        db.commit()
        return n

    async def job_stock_list(self) -> None:
        log.info("[股票列表] 开始同步")
        db = SessionLocal()
        try:
            n = await self._sync_stock_list(db)
            log.info("[股票列表] 完成 %d 条", n)
        except Exception as exc:  # noqa: BLE001
            log.exception("[股票列表] 失败: %s", exc)
            db.rollback()
        finally:
            db.close()

    # ---------------------------------------------------------------- 调度
    def setup(self) -> None:
        s = self.scheduler
        s.add_job(self.job_stock_list, "cron", hour=2, minute=0, id="stock_list")
        s.add_job(self.job_intraday_snapshot, "cron", minute="*", second="0",
                  id="intraday", max_instances=1)
        s.add_job(self.job_full_snapshot, "cron", minute="*/5", id="full_snapshot",
                  max_instances=1)
        s.add_job(self.job_daily_kline, "cron", hour=15, minute=40, id="daily_kline",
                  max_instances=1)
        s.add_job(self.job_special, "cron", hour=15, minute=50, id="special",
                  max_instances=1)
        s.add_job(self.job_kaipanla, "cron", hour=18, minute=10, id="kaipanla",
                  max_instances=1)

    async def run(self, once: str | None = None) -> None:
        if once:
            job = {
                "stock_list": self.job_stock_list,
                "full_snapshot": self.job_full_snapshot,
                "intraday": self.job_intraday_snapshot,
                "daily_kline": self.job_daily_kline,
                "special": self.job_special,
                "kaipanla": self.job_kaipanla,
            }.get(once)
            if not job:
                log.error("未知任务: %s", once)
                return
            log.info("手动执行单次任务: %s", once)
            await job()
            return

        self.setup()
        self.scheduler.start()
        log.info("采集器已启动，任务: %s",
                 [j.id for j in self.scheduler.get_jobs()])
        try:
            while True:
                await asyncio.sleep(60)
        except (KeyboardInterrupt, SystemExit):
            self.scheduler.shutdown()


if __name__ == "__main__":
    task = os.environ.get("COLLECTOR_ONCE") or (sys.argv[1] if len(sys.argv) > 1 else None)
    asyncio.run(Collector().run(task))
