"""签到积分与档位兑换。

整套机制里**没有任何收款环节** —— 没有金额、没有订单、没有支付渠道。
积分只能靠签到获得，见 app/points.py 顶部的三条合规红线。

接口：
  签到     POST /points/checkin
  概览     GET  /points/me
  流水     GET  /points/logs
  兑换     POST /points/redeem
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app import points as P
from app.schemas import ApiResponse, RedeemIn

router = APIRouter(prefix="/points", tags=["积分"])


@router.post("/checkin", response_model=ApiResponse, summary="每日签到")
def do_checkin(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """每个自然日（北京时间）可签到一次，得 1 积分。重复签到不重复给分。"""
    r = P.checkin(db, user)
    if r["already"]:
        return ApiResponse(msg="今天已经签到过了，明天再来", data=r)
    return ApiResponse(msg=f"签到成功，+{r['gained']} 积分", data=r)


@router.get("/me", response_model=ApiResponse, summary="我的积分与可兑换档位")
def my_points(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """控制台「积分」区块的数据源：余额、今天能否签到、各档位还差多少分。"""
    return ApiResponse(data=P.summary(db, user))


@router.get("/logs", response_model=ApiResponse, summary="积分流水")
def my_logs(limit: int = Query(20, ge=1, le=100),
            db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    rows = P.recent_logs(db, user.id, limit)
    return ApiResponse(data={
        "items": [{
            "id": r.id,
            "type": r.type,
            "delta": r.delta,
            "balance_after": r.balance_after,
            "plan_code": r.plan_code,
            "expires_at": r.expires_at,
            "remark": r.remark,
            "created_at": r.created_at,
        } for r in rows],
    })


@router.post("/redeem", response_model=ApiResponse, summary="用积分兑换档位")
def do_redeem(payload: RedeemIn,
              db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """兑换 / 续期某个档位。积分不足或档位不可兑换时返回 400。"""
    try:
        r = P.redeem(db, user, payload.plan_code)
    except ValueError as e:
        #: 业务性拒绝走 400，不要让它冒泡成 500 ——
        #: 前端要能直接把这句话显示给用户看
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    exp = r["expires_at"]
    return ApiResponse(
        msg=f"{r['action']}成功：{r['plan']['name']}，有效期至 "
            f"{exp.strftime('%Y-%m-%d')}（{r['days']} 天）",
        data={
            "tier": r["plan"]["code"],
            "plan": r["plan"],
            "cost": r["cost"],
            "days": r["days"],
            "action": r["action"],
            "expires_at": exp,
            "points_left": r["points_left"],
        },
    )
