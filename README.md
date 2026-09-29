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

> **档位不是商品。** 开源版不做任何买卖，它只是管理员给不同用户分配额度的手段。

三档内置额度（可在后台自由增删改）：

| 档位 | API Key | 限流 | 日调用 |
|---|---|---|---|
| 默认档 `free` | 3 个 | 60 次/分钟 | 1,000 |
| 进阶档 `pro` | 10 个 | 300 次/分钟 | 50,000 |
| 宽松档 `vip` | 50 个 | 1200 次/分钟 | 不限 |

新注册用户落在默认档。管理员在后台「档位管理」改档位或给用户换档，**立即生效**。

- 限流是**按 Key 的滑动窗口**，配额是**按用户的日总量**，两者互补；
- `daily_quota = -1` 表示不限量；
- 超限时返回 `429`，响应头带 `Retry-After`。

---

## 数据源

| 数据源 | 接法 | 说明 |
|---|---|---|
| 通达信 | easy-tdx 自建服务 | 全市场日 K，稳定快 |
| 同花顺 | 官方开放接口 | 财报、估值、特色数据 |
| 开盘了 | 本地代理 | 涨停池、龙虎榜等情绪数据 |
| 东方财富 | 公开接口 | 资金流、板块 |
| 新浪 | 公开接口 | 快照兜底 |
| 腾讯 | 公开接口 | 快照兜底，批量查询快 |

适配器统一实现 `fetch()`，主源失败自动切备源，全部失败才报错。
新增数据源只需加一个适配器文件，不用动上层。

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
