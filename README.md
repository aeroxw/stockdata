# StockData

> 一个 API，汇通六大 A 股数据源 —— 开源、可自建、不卖任何套餐。

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](./LICENSE)

StockData 把 **通达信 / 同花顺 / 开盘了 / 东方财富 / 新浪 / 腾讯** 六家数据
收敛成统一模型，对外只暴露一套 REST 接口：行情快照、K 线、财报、涨停池、龙虎榜、
资金流…… 多源交叉校验、主备自动切换、分层缓存、按 Key 限流，全部内置。

本项目**代码以 Apache-2.0 开源**，不含任何付费墙、试用期或售卖逻辑。
「档位（plan）」只是管理员用来分配额度（Key 数 / 限流 / 日配额）的运营手段 ——
这层管控不能省，否则一个注册账号就能把六家上游撸到集体封 IP。

---

## 目录

- [为什么有这个项目](#为什么有这个项目)
- [快速开始](#快速开始)
- [接口一览](#接口一览)
- [配额与档位](#配额与档位)
- [签到积分](#签到积分)
- [数据源](#数据源)
- [项目结构](#项目结构)
- [本地开发](#本地开发)
- [常见问题](#常见问题)
- [数据与合规](#数据与合规)
- [许可证](#许可证)

---

## 为什么有这个项目

自己接单一数据源，最贵的从来不是写那几行请求代码，而是长期运维：

| | 直接对接单一数据源 | 用 StockData |
|---|---|---|
| 容错 | 挂了就断，得自己写主备 | 六源主备自动切换 |
| 正确性 | 无法自证，错了也不知道 | 多源交叉校验，偏差超阈值告警 |
| 缓存与限流 | 全部自建，否则容易封 IP | 内置分层缓存 + 按 Key 滑动窗口限流 |
| 密钥与配额 | 无 | 控制台自助创建/轮换/吊销，按档位配额 |
| 特色数据 | 分散在各家，字段口径不一 | 统一出口，龙虎榜双源可交叉比对 |

---

## 快速开始

### 用 Docker 起全套（推荐）

```bash
git clone https://github.com/aeroxw/stockdata.git
cd stockdata
cp .env.example .env          # 至少改 POSTGRES_PASSWORD 与 JWT_SECRET
docker compose up -d --build
```

然后打开 <http://localhost:9850>：

- `/` 首页 · `/docs.html` 接口文档 · `/login.html` 注册登录 · `/console.html` 控制台
- `/admin.html` 管理后台（**第一个注册的用户自动成为管理员**）
- `/api/docs` 交互式 OpenAPI

一键起五个容器：`postgres` / `redis` / `api` / `collector` / `nginx`，
宿主端口 **9850**。

### 不用 Docker

```bash
cd backend
python -m venv .venv && ./.venv/Scripts/activate      # Windows
pip install -r requirements.txt
cp ../.env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 9850
```

默认 DATABASE_URL 会落到 `backend/stockdata.db`（SQLite），开箱即可跑；
生产请改成 PostgreSQL。Redis 不可用时会自动降级为进程内限流，不会拦住启动。

### 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `POSTGRES_PASSWORD` | ✅ | 数据库密码，`docker-compose.yaml` 里的 `change_me` 只是占位 |
| `JWT_SECRET` | ✅ | `openssl rand -base64 48` 生成；换掉会让所有已登录用户掉线 |
| `HITHINK_API_KEY` | 选填 | 留空时特色数据（涨停池/龙虎榜）不可用，其余五家照常 |
| `HITHINK_BASE` | 选填 | 默认 `https://fuyao.aicubes.cn` |
| `EASYTDX_BASE` | 选填 | 你自己搭的 easy-tdx 服务地址 |
| `KPL_PROXY_BASE` | 选填 | 你自己搭的开盘了代理地址 |
| `DATABASE_URL` / `REDIS_URL` | 选填 | 容器编排里已由 compose 注入 |

---

## 接口一览

统一信封：`{"code":0,"msg":"ok","data":{...}}`。业务数据永远在 `data` 里。

鉴权三选一：`Authorization: Bearer sk_live_xxx` / `X-API-Key: sk_live_xxx` / `?apikey=sk_live_xxx`。

### 公开接口（无需登录）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/public/quote` | 单只/多只行情快照 |
| GET | `/api/v1/public/market` | 市场总览（指数、涨跌家数） |
| GET | `/api/v1/sources` | 六家数据源实时健康度 |

### 需要 API Key

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/quote` | 行情快照（多源聚合） |
| GET | `/api/v1/kline` | 日/周/月/分钟 K 线，支持前复权 |
| GET | `/api/v1/special/{kind}` | 特色数据：涨停池、炸板池、龙虎榜、资金流等 |
| GET | `/api/v1/special` | 全部特色数据 |
| GET | `/api/v1/stock/list` | 全市场标的清单 |

### 配额与用量

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/quota/plans` | 公开的额度档位列表 |
| GET | `/api/v1/quota/me` | 我的档位与额度使用情况 |
| GET | `/api/v1/quota/usage` | 近 N 天调用曲线、错误率、Top 路径 |

### 账号与密钥

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/register` | 注册（首个用户自动为管理员） |
| POST | `/api/v1/auth/login` | 登录，返回 access + refresh |
| POST | `/api/v1/auth/refresh` | 续期 |
| GET/POST/DELETE | `/api/v1/apikey` | 密钥列表 / 创建 / 吊销 |
| POST | `/api/v1/apikey/{id}/rotate` | 轮换 |
| DELETE | `/api/v1/apikey/{id}/purge` | 永久删除 |

### 管理后台（需管理员）

`/api/v1/admin/*`：`stats` `users` `keys` `logs` `plans` `redis/recheck`。

### 一行试试

```bash
curl "http://localhost:9850/api/v1/quote?codes=600519.SH" \
  -H "Authorization: Bearer sk_live_你的密钥"
```

---

## 配额与档位

> **档位不是商品。** 开源版不做任何买卖 —— 用户不用花钱，也不能花钱。

三档内置额度（可在后台自由增删改）：

| 档位 | API Key | 限流 | 日调用 | 签到兑换 |
|---|---|---|---|---|
| 免费版 `free` | 3 个 | 60 次/分钟 | 1,000 | —（永久免费） |
| 专业版 `pro` | 10 个 | 300 次/分钟 | 50,000 | 15 积分 / 30 天 |
| 旗舰版 `vip` | 50 个 | 1200 次/分钟 | 不限 | 30 积分 / 30 天 |

新注册用户落在免费版，**永久可用**。管理员也可以在后台直接给用户指派任意档位。

- 限流是**按 Key 的滑动窗口**，配额是**按用户的日总量**，两者互补；
- `daily_quota = -1` 表示不限量；
- 超限时返回 `429`，响应头带 `Retry-After`。

---

## 签到积分

更高额度唯一的获取方式是**每天来签到一次**：签到 +1 积分，攒够即可兑换。

这么设计不是为了赚钱，是为了**保护上游六家数据源**：
15 天连续签到才换一个月专业版，门槛是耐心，不是钱。
挡住的是"注册完就甩个脚本、几分钟把六家源撸到集体封 IP"的人，
让额度流向真正长期使用的人。

### 三条不能破的红线

这三条是整套机制合法性的地基，改动前请务必回头对照：

1. **积分不能买。** 系统里不存在任何充值入口。
   一旦积分能用钱买到，它就有了财产价值，用它换额度等同于变相售卖，
   法律性质从"无偿的运营手段"变成"有偿互联网信息服务"——那是要 ICP 许可证的。
2. **积分不能转让。** 不开放用户间赠送、交易、转移。
   能转让就形成二级市场，反过来证明它有财产价值。
3. **到期只降档，不停服。** 档位到期自动回落免费版，接口照常调用。
   一旦变成"不给兑换就停服"，性质就接近付费墙了。

### 规则

- 每个自然日（北京时间）可签到一次，得 1 分；重复签到不加分也不产生流水；
- 同档再兑换 = **续期**，在剩余天数上顺延，不会亏掉已攒的天数；
- 换成更高的档 = 从今天起重新算一个月；
- 不允许降级兑换；
- 管理员可手动补发/扣减积分（运营通道，不涉及金额），每笔都进 `point_logs`。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/points/checkin` | 每日签到 |
| GET | `/api/v1/points/me` | 积分余额、今日能否签到、各档位还差多少 |
| GET | `/api/v1/points/logs` | 积分流水 |
| POST | `/api/v1/points/redeem` | 兑换 / 续期档位 |
| POST | `/api/v1/admin/users/{id}/points` | 管理员调整积分 |

---

## 数据源

| 数据源 | 接法 | 说明 |
|---|---|---|
| 通达信 | easy-tdx 自建服务 | 全市场日 K，稳定快 |
| 同花顺 | 官方开放接口 | 财报、估值、特色数据 |
| 开盘了 | 本地代理 | 涨停池、龙虎榜等情绪数据 |
| 东方财富 | 公开接口 | 资金流、板块（**见下方说明**） |
| 新浪 | 公开接口 | 快照兜底 |
| 腾讯 | 公开接口 | 快照兜底，批量查询快 |

适配器统一实现 `fetch()`，主源失败自动切备源，全部失败才报错。
新增数据源只需加一个适配器文件，不用动上层。

### 关于东方财富的已知情况

东财的快照接口在 2026-09-29 换过一次端点：老的 `/api/qt/stock/get`（一只一个请求）
已经取不到数了，现在改走批量的 `/api/qt/ulist.np/get`（50 只一个请求）。
另外它的限流策略很特别 —— **按出口 IP，在 TCP 层直接掐断连接，不返回 429**，
判据是"短时间内的请求条数"。所以：

- 网页是正常的（`www.eastmoney.com` 200），只有 `/api/qt/*` 的数据请求会被丢；
- 一旦被判限流，**重试没有任何用**，静默 120s 也恢复不了，越敲拖得越久；
- 因此东财适配器设成 `net_retries=0`、失败即熔断（冷却 5 分钟起步、上限 1 小时），
  并且排在两条链的最后一位当兜底 —— 它挂了不影响取数，只是后台面板上会显示「异常」。

自建时如果发现东财长期显示「异常」，多半就是出口 IP 在限流窗口里，
等一段时间或者换个出口即可，**不是部署哪里配错了**。

---

## 项目结构

```
.
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI 入口，路由挂载
│   │   ├── config.py        # 环境变量（pydantic-settings）
│   │   ├── models.py        # SQLAlchemy 模型
│   │   ├── db.py            # 会话 + 建表 + 列升级
│   │   ├── deps.py          # 鉴权 / 限流 / 配额依赖
│   │   ├── plans.py         # 档位（额度），非商品
│   │   ├── ratelimit.py     # Redis 滑动窗口，自动降级
│   │   ├── adaptors/        # 六家数据源适配器 + 聚合
│   │   ├── routers/         # auth / apikey / data / quota / admin
│   │   └── static/          # 首页、控制台、后台、文档（原生 HTML/JS）
│   ├── Dockerfile
│   └── requirements.txt
├── collector/               # 独立采集容器，生命周期与 API 解耦
├── nginx/nginx.conf         # 反向代理到 api:9850
├── docker-compose.yaml
├── .env.example
├── LICENSE                  # Apache-2.0
└── NOTICE                   # 第三方依赖与数据来源声明
```

前端是原生 HTML/CSS/JS，没有构建步骤 —— 改完刷新即可。

---

## 本地开发

```bash
cd backend
uvicorn app.main:app --reload --port 9850     # 热重载
python -m compileall app                      # 语法自检
```

改了后端代码后，**Docker 部署必须重新 build**（代码是 COPY 进镜像的，没有 bind mount）：

```bash
docker compose up -d --build api
```

`tools/` 下有一些取舍用的脚本：`healthcheck.py`（接口体检）、
`upload.py` / `deploy.py`（上传到 NAS 并重建）、`check_js.py`（前端语法自检）。

---

## 常见问题

**Q：为什么没有「购买套餐」页面？**
因为这是开源项目，不做任何售卖。额度由部署方（你）在后台分配。

**Q：我能不能基于它做商业产品？**
可以。Apache-2.0 允许商用、修改、分发，只要保留版权与许可证声明、
标注改动即可，无 copyleft 传染。但**上游数据的授权要你自己搞定**（见下节）。

**Q：六家数据源都要配吗？**
不用。`EASYTDX_BASE` / `KPL_PROXY_BASE` 指向的服务需要你自己搭，
没搭就不填，对应数据源自动降级，其余照常工作。`HITHINK_API_KEY` 同理。

**Q：改了后台代码没生效？**
Docker 部署下代码是 COPY 进镜像的，`docker restart` 没用，必须
`docker compose up -d --build`。另外 nginx 会缓存 upstream DNS，
api 重建后要跟着重启 nginx，否则会 502。

---

## 数据与合规

**本项目只开源代码，不开源数据，也不分发数据。**

- 所有行情、K 线、财报、特色数据都是运行时从六家上游的公开接口取得，
  其使用受各自服务条款约束；
- 自部署时数据在你的网络内直接流向你的用户，不经本项目作者之手；
- 若你对外提供服务，请自行确认符合上游条款与所在地法律法规
  （在中国大陆涉及经营性信息服务需关注 ICP 备案 / 许可证要求）；
- 行情数据存在延迟与误差可能，**不构成任何投资建议**，作者不对数据准确性作担保。

详见 [NOTICE](./NOTICE)。

---

## 许可证

[Apache License 2.0](./LICENSE) © 2026 StockData contributors

依赖的第三方库均为 MIT / BSD / Apache 等宽松协议 —— 见 [NOTICE](./NOTICE)。
