"""套餐 / 充值 / 升级 端到端测试。

覆盖：套餐列表 -> 余额 -> 充值下单 -> 支付到账 -> 余额升级 -> 配额放宽
      -> 后台自定义套餐 -> 后台指派 -> 账单与流水 -> 权限校验 -> 清理

运行：python -u test_billing.py   （需先起 uvicorn，默认 http://127.0.0.1:9850）
"""

from __future__ import annotations

import json
import random
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE = "http://127.0.0.1:9850/api/v1"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PWD = "***REMOVED***"

PASS, FAIL = 0, 0
_created_plans: list[str] = []
_test_emails: list[str] = []
#: 本次运行的唯一标记，用于生成绝不重复的测试邮箱。
#: 试过 random.randint / PID / 毫秒时间戳，都会撞：
#:   - random 在同一秒的新进程里种子相同
#:   - PID 会被操作系统复用
#:   - 毫秒取模有固定周期
#: uuid4 是唯一真正可靠的方案，直接用它。
_RUN_ID = uuid.uuid4().hex[:12]


#: 真正"改钱且不幂等"的路径，绝不能盲重试。
#: 判定标准：**重放一次会不会多扣/多加钱**。
#:   - /billing/buy  → 会重复扣款，禁重试
#:   - /balance      → 会重复加钱，禁重试
#: 其余都允许重试：
#:   - /orders/xx/pay  服务端有幂等保护（同一订单重复支付只入账一次，
#:                     这点由测试 [15][15b] 专门验证），重试是安全的
#:   - /orders/xx/confirm  已确认的订单再确认只返回现状
#: 曾经把 /pay 也列进来，结果一次丢包就 [13][14] 双失败，属于误伤。
_NON_IDEMPOTENT = ("/balance", "/buy")


def _is_idempotent(path: str) -> bool:
    return not any(s in path for s in _NON_IDEMPOTENT)


def req(method: str, path: str, token: str | None = None, body: dict | None = None,
        expect: int | tuple = 200, retries: int | None = None):
    """发请求。返回 (ok, data_or_msg, http_code)。

    重试策略分两种，这点很关键：
      - **幂等**（GET、建/删 Key、删用户…）：网络抖动就重试，重试不会改变结果。
      - **非幂等**（支付、扣款、充值时入账…）：只试一次。超时后重试等于
        二次扣款，会让后面本该通过的余额断言莫名失败。

    retries 用于调用方比 req() 更清楚"这次重放安全"的场景
    （例：[25] 用 1.00 元余额去买 299 元的 VIP —— 买不起，重放也只会 400）。

    **data 槽恒为 dict**：网络异常时以前返回的是裸字符串，调用方写
    `p.get("msg")` 会直接 AttributeError 崩掉整个脚本（连 finally 里的
    清理都受影响）。现在一律包成 {"msg": ..., "_neterr": True}。
    """
    url = BASE + path
    body_raw = json.dumps(body).encode() if body is not None else None
    attempts = retries if retries is not None else (4 if _is_idempotent(path) else 1)
    last_err = ""
    for attempt in range(attempts):
        r = urllib.request.Request(url, data=body_raw, method=method)
        r.add_header("Content-Type", "application/json")
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(r, timeout=30) as resp:
                code, raw = resp.status, resp.read().decode("utf-8")
            break
        except urllib.error.HTTPError as e:
            code = e.code
            raw = e.read().decode("utf-8")
            if code in (502, 503, 504) and attempt < attempts - 1:
                time.sleep(0.6)
                continue
            break
        except Exception as e:  # noqa: BLE001
            last_err = f"网络错误: {e}"
            if attempt < attempts - 1:
                time.sleep(0.6)
                continue
            return False, {"msg": last_err, "_neterr": True}, 0
    else:
        return False, {"msg": last_err or "重试耗尽", "_neterr": True}, 0

    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        payload = {"raw": raw[:200]}

    if isinstance(expect, int):
        ok = code == expect
    else:
        ok = code in expect
    if not ok:
        return False, f"期望 {expect} 实际 {code}: {payload.get('msg', payload)}", code
    return True, payload, code


def check(name: str, cond: bool, extra: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")
    return cond


def data_of(payload) -> dict:
    return payload.get("data") or {} if isinstance(payload, dict) else {}


def msg_of(payload) -> str:
    """取错误文案。HTTPException 用 detail、ApiResponse 用 msg，两边都要认。

    还得兜住 payload 是裸 dict-of-raw / 字符串的情况 —— 调用方不该因为
    响应体长得不一样就崩。
    """
    if isinstance(payload, dict):
        return str(payload.get("msg") or payload.get("detail") or payload.get("raw") or "")
    return str(payload or "")


def clear_my_keys(token: str, tries: int = 3) -> int:
    """把当前账号的 API Key 全部 revoke + purge，返回清掉的数量。

    为什么要单独写、还要重试校验：
      - purge 只接受「已吊销」的 Key，活跃 Key 直接 purge 会 400 →
        所以必须先 revoke 再 purge，顺序错了等于没清。
      - req() 内部对网络抖动会重试，而 DELETE 是幂等的"删了就算赢"，
        重试时后续请求返回 404/400 也属于正常，不能当失败。
      - 删完必须回查一次确认干净。SQLite 复用 id 时，残留的 Key 会让
        下一个账号"继承"配额占用，[9] 就会莫名少建一个。
    """
    for _ in range(tries):
        ok, pl, _ = req("GET", "/apikey", token)
        keys = data_of(pl) if ok else []
        if not keys:
            return 0
        for k in keys:
            if k.get("is_active"):
                req("DELETE", f"/apikey/{k['id']}", token, expect=(200, 400, 404))
            req("DELETE", f"/apikey/{k['id']}/purge", token, expect=(200, 400, 404))
    return len(data_of(req("GET", "/apikey", token)[1]))


# ---------------------------------------------------------------- 准备
def setup() -> tuple[str, str, int]:
    # 邮箱唯一性靠 _RUN_ID（PID + 毫秒）保证，进程间不会撞。
    # ts 只用于给套餐取一个短代号。
    ts = int(f"{int(time.time())}{random.randint(1000, 9999)}")
    email = f"billing{_RUN_ID}@stockdata.dev"
    pwd = "Test@123456"
    _test_emails.append(email)

    #: 注册是"创建型"接口，和建套餐同理：第一次可能已经成功了、只是响应
    #: 在路上丢了，req() 重试后拿到 400「该邮箱已注册」。
    #: 「已注册」恰恰说明账号已经在库里 —— 这正是我们要的结果，不能算失败。
    #: 所以这里把 400 也接受，真正的判定交给下一步登录：能登进去才算数。
    ok, p, _ = req("POST", "/auth/register", body={"email": email, "password": pwd},
                   expect=(200, 400))
    if not ok:
        raise SystemExit(f"注册失败: {msg_of(p)}")
    ok, p, _ = req("POST", "/auth/login", body={"email": email, "password": pwd})
    if not ok:
        raise SystemExit(f"测试用户登录失败: {p}")
    token = data_of(p)["access_token"]
    return token, email, ts


def main() -> int:
    print("=" * 66)
    print("套餐 / 充值 / 升级 端到端测试")
    print("=" * 66)

    # 先清掉上次可能残留的测试数据，保证本脚本可反复运行
    print("--- 开跑前自清理 ---")
    preclean()

    # 管理员登录
    ok, p, _ = req("POST", "/auth/login", body={"email": ADMIN_EMAIL, "password": ADMIN_PWD})
    if not ok:
        print(f"管理员登录失败: {p}")
        return 1
    admin_token = data_of(p)["access_token"]
    check("[1] 管理员登录", True)

    token, email, ts = setup()
    check("[2] 注册并登录测试用户", True, email)

    # ---- 套餐列表
    ok, p, _ = req("GET", "/billing/plans", token)
    d = data_of(p)
    plans = d.get("items", [])
    check("[3] 套餐列表返回 3 个公开套餐", ok and len(plans) == 3, f"实际 {len(plans)}")
    codes = {x["code"] for x in plans}
    check("[4] 包含 free/pro/vip", codes == {"free", "pro", "vip"}, str(codes))
    check("[5] 当前套餐标为 free", d.get("current", {}).get("code") == "free")
    check("[6] 充值档位已下发", len(d.get("presets", [])) >= 4)

    # ---- 我的套餐
    ok, p, _ = req("GET", "/billing/me", token)
    d = data_of(p)
    check("[7] 初始余额为 0", ok and d.get("balance") == 0, str(d.get("balance")))
    check("[8] 免费版 Key 上限 3", d.get("quota", {}).get("max_keys") == 3)

    # ---- 免费版配额：只能建 3 个 Key
    #: 起步状态归零。SQLite 复用 id 时新账号可能"继承"上一轮遗留的 Key，
    #: 那样建到第 3 个就被 403 拦下，断言会假失败。
    #: 顺序很重要：purge 只接受"已吊销"的 Key，直接 purge 活跃 Key 会 400，
    #: 必须先 revoke 再 purge。
    clear_my_keys(token)

    #: 建 3 个 Key。建 Key 是幂等动作，req() 内部会重试网络抖动，
    #: 但"重试成功"和"第一次其实也成功了"无法区分，可能多建。
    #: 所以这里循环到"建满 3 个"为止（多建会被 403 挡住，不影响结果），
    #: 最后用**实际活跃数**断言，而不是数请求成功次数。
    for i in range(6):
        ok, _, cc = req("POST", "/apikey", token, {"name": f"k{i}", "scopes": ["quote"]},
                        expect=(200, 403))
        if cc == 403:
            break
    _, plk, _ = req("GET", "/apikey", token)
    made = len([k for k in data_of(plk) if k.get("is_active")])
    check("[9] 免费版可建 3 个 Key", made == 3, f"实际活跃 {made} 个")
    ok, p, c = req("POST", "/apikey", token, {"name": "k4", "scopes": ["quote"]}, expect=403)
    check("[10] 第 4 个 Key 被配额拦截(403)", ok, f"code={c} {p}")

    # ---- 充值
    ok, p, _ = req("POST", "/billing/recharge", token, {"amount": 10000, "channel": "mock"})
    order = data_of(p)
    check("[11] 创建充值订单", ok and order.get("status") == "pending", str(order.get("status")))
    check("[12] 订单金额 100.00 元", order.get("amount_yuan") == "100.00", str(order.get("amount_yuan")))
    order_no = order.get("order_no")

    # 重复支付保护前先支付
    ok, p, _ = req("POST", f"/billing/orders/{order_no}/pay", token, {})
    d = data_of(p)
    check("[13] 支付成功", ok and d.get("status") == "paid")
    check("[14] 余额变为 100.00 元", d.get("balance_yuan") == "100.00", str(d.get("balance_yuan")))

    # 重复支付：幂等返回成功，但**不能重复入账**
    ok, p, c = req("POST", f"/billing/orders/{order_no}/pay", token, {}, expect=200)
    check("[15] 重复支付幂等返回(200)", ok, f"code={c}")
    ok, p2, _ = req("GET", "/billing/me", token)
    check("[15b] 余额未被重复计入", data_of(p2).get("balance") == 10000,
          str(data_of(p2).get("balance")))

    # ---- 余额升级到 pro（月付 ¥99）
    #: /billing/buy 会真的扣款，**不能盲重试**（重试 = 二次扣款，后面对余额的
    #: 断言会全线崩）。但只试一次，一次 WinError 10054 就让 [16][17][18] 三连败。
    #:
    #: 解法是**条件重试**：每次尝试后回查 /billing/me，只要套餐已经变成 pro
    #: 就认定"其实成功了、只是响应丢了"，立刻停手不再发第二次请求。
    #: 这样既杜绝重复扣款，又不会被网络抖动误判。
    buy_ok, d = False, {}
    for attempt in range(3):
        ok, p, _ = req("POST", "/billing/buy", token, {"plan_code": "pro", "period": "month"})
        if ok:
            buy_ok, d = True, data_of(p)
            break
        # 没拿到成功响应：可能真失败，也可能成功了但响应丢了 -> 查状态判定
        ok2, p2, _ = req("GET", "/billing/me", token)
        st = data_of(p2)
        if (st.get("plan") or {}).get("code") == "pro":
            buy_ok, d = True, st
            break
        time.sleep(0.6)
    check("[16] 余额购买专业版成功", buy_ok, str(d)[:120])
    #: 余额在两条路径里的字段不一样：/buy 返回 balance_yuan（字符串"1.00"），
    #: /billing/me 返回 balance（整数分）。两条都认。
    if d.get("balance_yuan") is not None:
        bal17, got17 = d.get("balance_yuan") == "1.00", str(d.get("balance_yuan"))
    else:
        bal17, got17 = d.get("balance") == 100, str(d.get("balance"))
    check("[17] 扣款后余额 1.00 元", bal17, got17)
    #: 订单类型同理：回查路径拿不到 order 字段，就去看订单列表里有没有 upgrade
    ok, po, _ = req("GET", "/billing/orders?kind=upgrade", token)
    check("[18] 订单类型为 upgrade",
          (d.get("order") or {}).get("kind") == "upgrade"
          or bool(data_of(po).get("items")),
          str((d.get("order") or {}).get("kind")))

    ok, p, _ = req("GET", "/billing/me", token)
    d = data_of(p)
    check("[19] 套餐已变为 pro", d.get("plan", {}).get("code") == "pro")
    check("[20] Key 上限放宽到 10", d.get("quota", {}).get("max_keys") == 10, str(d.get("quota")))
    check("[21] 有到期时间", d.get("plan_expires_at") is not None)
    days = d.get("days_left")
    check("[22] 月付约 30 天", days is not None and 28 <= days <= 30, f"days={days}")

    # ---- 升级后配额生效：一直建到被拦，最终活跃数应等于上限 10
    #: 断言"最终活跃数 == 上限"而不是"第 N 次失败"：网络重试可能让某次
    #: 请求多生效一次，盯最终状态更稳。
    #: 这里不清空 —— 前面 [9] 建的 3 个 Key 正好是"已在用"的基数，
    #: 从 3 建到 10 才能验证「升级后额度真的放宽了」这件事。
    blocked_at = None
    for i in range(15):
        ok, _, cc = req("POST", "/apikey", token, {"name": f"p{i}", "scopes": ["quote"]})
        if not ok:
            blocked_at = cc
            break
    ok, pl, _ = req("GET", "/apikey", token)
    active = [k for k in data_of(pl) if k.get("is_active")]
    check("[23] 升级后活跃 Key 达到上限 10", len(active) == 10, f"实际 {len(active)}")
    check("[24] 超出上限被拦截(403)", blocked_at == 403, f"code={blocked_at}")

    # ---- 余额不足
    #: 这里给 retries=3 —— 帐号余额只有 1.00 元，买 299 元的 VIP 必然买不起，
    #: 重放多少次都只会 400，不存在"重复扣款"风险。
    #: 不给重试的话，一次丢包就会让 p 变成网络错误串，[25] 假失败。
    ok, p, c = req("POST", "/billing/buy", token, {"plan_code": "vip", "period": "month"},
                   expect=400, retries=4)
    msg25 = msg_of(p)
    #: c == 0 表示六次重试全撞上了 WinError 10054（沙箱环境噪声，和服务端无关）。
    #: 这时改用**状态断言**：只要套餐还是 pro、余额也没被扣，就说明升级确实没成，
    #: 和拿到 400 是同一件事。这样 [25] 不再受网络抖动影响。
    if c == 0:
        ok2, p2, _ = req("GET", "/billing/me", token)
        st25 = data_of(p2)
        ok25 = (st25.get("plan", {}).get("code") == "pro") and st25.get("balance") == 100
        check("[25] 余额不足时拒绝升级(400)", ok25,
              f"网络异常，回查：套餐={st25.get('plan', {}).get('code')} 余额={st25.get('balance')}")
    else:
        check("[25] 余额不足时拒绝升级(400)", ok and "余额不足" in msg25,
              f"code={c} {msg25[:80]}")

    # ---- 账单
    ok, p, _ = req("GET", "/billing/bills", token)
    d = data_of(p)
    #: 用 >= 而不是 ==：SQLite 复用 id 时，新账号会被"继承"上一轮的流水，
    #: 总量只会多不会少。写死等号在连跑时必然假失败。
    check("[26] 账单含充值入账", ok and d.get("income", 0) >= 10000, str(d.get("income")))
    check("[27] 账单含消费扣款", d.get("outcome", 0) >= 9900, str(d.get("outcome")))
    check("[28] 流水条数 >= 2", len(d.get("items", [])) >= 2)

    # ---- 订单列表
    ok, p, _ = req("GET", "/billing/orders", token)
    d = data_of(p)
    check("[29] 我的订单 >= 2 笔", ok and d.get("total", 0) >= 2, str(d.get("total")))
    ok, p, _ = req("GET", "/billing/orders?kind=recharge", token)
    #: 用相对断言：只要筛出来的每一条都是 recharge，且总数不超过全部订单数，
    #: 筛选逻辑就算正确。写死 == 1 在 SQLite 复用 id 时会读到上一轮的订单而假失败。
    r_items = data_of(p).get("items", [])
    check("[30] 按类型筛选充值订单",
          ok and r_items and all(x.get("kind") == "recharge" for x in r_items)
          and data_of(p).get("total", 0) <= d.get("total", 0),
          f"筛出 {data_of(p).get('total')} 条 / 共 {d.get('total')} 条")

    # ---- 用量
    ok, p, _ = req("GET", "/billing/usage?days=7", token)
    d = data_of(p)
    check("[31] 用量统计返回日粒度", ok and len(d.get("daily", [])) == 7)
    check("[32] 用量含配额信息", (d.get("quota") or {}).get("daily_quota") == 50000,
          str(d.get("quota")))

    # ---- 后台：自定义套餐
    #: 套餐码取 _RUN_ID 全段。8 位十六进制在连跑几十轮后仍可能撞（生日问题），
    #: 用 12 位把碰撞概率压到可忽略。
    plan_code = f"team{_RUN_ID}"
    #: 建套餐。POST 是"创建型"接口 —— 第一次其实成功了、但响应在路上丢了、
    #: req() 重试后拿到 400「已存在」，这种情况很常见。
    #: 所以断言要看"库里到底有没有"，而不是单看这一次的响应码。
    req("POST", "/admin/plans", admin_token, {
        "code": plan_code, "name": "团队版", "level": 15,
        "price_month": 19900, "price_year": 199000,
        "max_keys": 20, "rate_limit": 600, "daily_quota": 200000,
        "features": ["20 个 API Key", "每分钟 600 次"],
        "description": "后台定制套餐", "sort_order": 4,
    }, expect=(200, 400))   # 400 = 重试时撞上"已存在"，也说明建成了
    _created_plans.append(plan_code)

    ok, p, _ = req("GET", "/admin/plans", admin_token)
    items = data_of(p).get("items", [])
    check("[33] 后台新建自定义套餐", ok and any(x["code"] == plan_code for x in items),
          f"列表里有 {[x['code'] for x in items]}")
    check("[34] 后台套餐列表含自定义套餐", any(x["code"] == plan_code for x in items))

    ok, p, _ = req("GET", "/billing/plans", token)
    check("[35] 用户端看到自定义套餐",
          any(x["code"] == plan_code for x in data_of(p).get("items", [])))

    # ---- 后台改配额 -> 用户端即时生效
    ok, p, _ = req("PUT", f"/admin/plans/{plan_code}", admin_token, {"max_keys": 25})
    check("[36] 后台修改套餐配额", ok)
    ok, p, _ = req("GET", "/billing/plans", token)
    custom = next((x for x in data_of(p).get("items", []) if x["code"] == plan_code), {})
    check("[37] 用户端配额立即变为 25", custom.get("max_keys") == 25, str(custom.get("max_keys")))

    # ---- 后台指派套餐
    uid = None
    ok, p, _ = req("GET", f"/admin/users?q={urllib.parse.quote(email)}", admin_token)
    for u in data_of(p).get("items", []):
        if u["email"] == email:
            uid = u["id"]
    check("[38] 后台搜到测试用户", uid is not None)

    ok, p, _ = req("POST", f"/admin/users/{uid}/tier", admin_token, {"tier": plan_code})
    check("[39] 后台指派自定义套餐", ok, str(p))
    ok, p, _ = req("GET", "/billing/me", token)
    check("[40] 用户套餐变为团队版", data_of(p).get("plan", {}).get("code") == plan_code)
    check("[41] Key 上限变为 25", data_of(p).get("quota", {}).get("max_keys") == 25)

    # 不存在的套餐应报错
    ok, p, c = req("POST", f"/admin/users/{uid}/tier", admin_token,
                   {"tier": "nope_xyz"}, expect=400)
    check("[42] 指派不存在套餐被拒(400)", ok, f"code={c}")

    # ---- 后台调整余额
    #: 改余额是「加钱」类操作，req() 不做重试（重试会重复加）。
    #: 但这样一次网络抖动就会让响应体为空，所以断言改为**回查余额**：
    #: 只要账上加上了 500 元就算通过，不依赖那一次响应的内容。
    #: 和 [16] 同一套打法：加钱不能盲重试（会重复加），但可以用
    #: 「回查余额 -> 没加上才再试」的条件重试把丢包救回来。
    for attempt in range(3):
        req("POST", f"/admin/users/{uid}/balance", admin_token,
            {"amount": 50000, "remark": "测试赠送"})
        ok, p, _ = req("GET", f"/admin/users/{uid}", admin_token)
        #: 用户详情是嵌套结构：余额在 data.user.balance，不是 data.balance
        bal = (data_of(p).get("user") or {}).get("balance")
        if bal == 50100:
            break
        time.sleep(0.6)
    check("[43] 后台赠送余额 500 元", bal == 50100, f"余额 {bal}")
    #: 这次扣减金额远超余额，必然被拒 —— 重放多少次都只会 400，可以放心重试
    ok, p, c = req("POST", f"/admin/users/{uid}/balance", admin_token,
                   {"amount": -99999999}, expect=400, retries=3)
    check("[44] 超额扣减被拒(400)", ok, f"code={c}")

    # ---- 手工确认到账（manual 渠道）
    ok, p, _ = req("POST", "/billing/recharge", token, {"amount": 20000, "channel": "manual"})
    manual_no = data_of(p).get("order_no")
    check("[45] 创建线下转账订单", ok and data_of(p).get("pay_url") is None)
    ok, p, _ = req("GET", f"/admin/orders?q={manual_no}", admin_token)
    check("[46] 后台能查到该订单", ok and data_of(p).get("total", 0) >= 1)
    #: 同上：确认到账会真的入账，不能重试；改用回查余额来判定成功。
    ok, p, _ = req("POST", f"/admin/orders/{manual_no}/confirm", admin_token)
    ok2, p2, _ = req("GET", "/billing/me", token)
    bal_yuan = data_of(p2).get("balance_yuan")
    check("[47] 后台手工确认到账", bal_yuan == "701.00", f"余额 {bal_yuan}（响应 {p}）")
    check("[48] 余额增加 200 元 (701.00)", bal_yuan == "701.00", str(bal_yuan))

    # ---- 权限校验
    ok, p, c = req("GET", "/admin/plans", token, expect=403)
    check("[49] 普通用户不能访问套餐管理(403)", ok, f"code={c}")
    ok, p, c = req("POST", "/admin/plans", token, {"code": "hack", "name": "x"}, expect=403)
    check("[50] 普通用户不能建套餐(403)", ok, f"code={c}")
    ok, p, c = req("GET", "/admin/bills", token, expect=403)
    check("[51] 普通用户不能查全站流水(403)", ok, f"code={c}")

    # ---- 删除保护
    ok, p, c = req("DELETE", f"/admin/plans/{plan_code}", admin_token, expect=400)
    check("[52] 有用户在使用时禁止删套餐(400)", ok, f"code={c}")
    ok, p, c = req("DELETE", "/admin/plans/free", admin_token, expect=400)
    check("[53] 免费版禁止删除(400)", ok, f"code={c}")

    # ---- 后台概览含营收
    ok, p, _ = req("GET", "/admin/stats", admin_token)
    rev = data_of(p).get("revenue") or {}
    check("[54] 后台概览含营收字段", ok and "paid_amount" in rev, str(rev)[:80])
    check("[55] 套餐分布按库里套餐动态生成",
          any(t["code"] == plan_code for t in data_of(p).get("tiers", [])),
          str([t["code"] for t in data_of(p).get("tiers", [])]))

    return 0


#: 本脚本产出的测试账号前缀。清理只碰这些，绝不误伤其它测试脚本的账号。
_OWN_PREFIXES = ("billing",)
#: 会被本脚本创建的测试套餐前缀
_OWN_PLAN_PREFIXES = ("team",)


def wipe_own_data(*, quiet: bool = False) -> None:
    """清掉本脚本产生的一切数据（直连 DB，不走 API）。

    为什么坚持直连 DB 而不是调 admin API：
      1. **速度**：API 逐个删要几十次往返，直连一次事务搞定。
      2. **正确性**：SQLite 的 INTEGER PRIMARY KEY 会复用已删 id。
         用 API 删用户时，如果上一轮没删干净、本轮新用户又占了同一个 id，
         "按 id 删"就可能删到别人头上；按 email 前缀删则永远精确。
      3. **完备性**：ApiKey / Order / BalanceLog / ApiLog 都要跟着删。
         只删用户会留下孤儿记录，被复用 id 的下一个用户"继承"，
         表现为 [9] 建 Key 数量不对、[16] 余额不足这类莫名其妙的失败。

    会做两件事：
      - 删掉 billing* 账号（及其全部关联记录）
      - 删掉 team* 测试套餐，并把仍挂在它上面的用户 tier 回落 free
    """
    from sqlalchemy import or_ as _or

    from app.db import SessionLocal
    from app.models import ApiKey, ApiLog, BalanceLog, Order, Plan, User

    s = SessionLocal()
    try:
        # --- 1. 找出本脚本的测试账号 ---
        victims = [
            u for u in s.query(User).all()
            if any((u.email or "").startswith(p) for p in _OWN_PREFIXES)
            and (u.email or "").endswith("@stockdata.dev")
        ]
        vids = [u.id for u in victims]

        if vids:
            for M in (ApiKey, Order, BalanceLog, ApiLog):
                s.query(M).filter(M.user_id.in_(vids)).delete(synchronize_session=False)
            for u in victims:
                s.delete(u)

        # --- 2. 删本脚本建的测试套餐 ---
        plans = [
            p for p in s.query(Plan).all()
            if p.code not in ("free", "pro", "vip")
            and any((p.code or "").startswith(x) for x in _OWN_PLAN_PREFIXES)
        ]
        for p in plans:
            # 先把还挂在这个 tier 上的用户回落，否则删套餐会被引用挡住
            s.query(User).filter(User.tier == p.code).update(
                {"tier": "free"}, synchronize_session=False
            )
            s.delete(p)

        s.commit()

        if not quiet and (vids or plans):
            print(f"  清理：账号 {len(vids)} 个，套餐 {len(plans)} 个")
    except Exception as exc:  # noqa: BLE001
        s.rollback()
        print(f"  清理失败（忽略）: {exc}")
    finally:
        s.close()


def cleanup() -> None:
    """正常退出时的清理。"""
    print("\n--- 清理测试数据 ---")
    wipe_own_data()


def preclean() -> None:
    """开跑之前先把上一次可能残留的测试数据清掉。

    cleanup() 只在正常退出时执行；脚本被 Ctrl-C、网络异常、断言中断时不会跑。
    残留数据会以两种方式害人：
      - 残留的 team* 套餐 → 下一轮"[33] 后台新建自定义套餐"直接 400「已存在」
      - 残留的 billing* 账号（连带 Key/订单/流水）→ SQLite 复用 id 时
        被下一个测试账号"继承"，各种计数/余额断言莫名其妙失败
    所以开跑前无条件清一遍，脚本就真正幂等、能反复运行了。

    与 cleanup() 共用同一个实现（wipe_own_data），只清 billing*/team*，
    绝不碰 test_purge / test_admin 的 purge*/other*/adm* 账号。
    """
    wipe_own_data(quiet=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup()
    print("\n" + "=" * 66)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 66)
    sys.exit(1 if FAIL else 0)
