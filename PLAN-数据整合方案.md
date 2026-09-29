# StockData 多源数据整合平台 — 最优方案 v2.0

> 版本 v2.0 · 2026-09-28（基于全部 6 源实测重写）
> v1.0 → v2.0 的关键变化：**同花顺 API 已实测打通且能力远超预期，从"备选"升为主力源；开盘了已部署代理服务并验证可用，从"待攻坚"变为"可用补充源"。6 家全部可用。**

---

## 一、先说结论（TL;DR）

**6 家数据源全部实测可用。** 架构：Python FastAPI 单体 + 独立采集器容器 + PostgreSQL + Redis，Docker Compose 部署到绿联 NAS。

v2.0 相比 v1.0 最重要的两个判断变化：

1. **同花顺是主力源，不是补充源。** 实测它有 94 个 REST 端点，且 `prices/snapshot` **一次请求返回全市场 5578 只**——这比腾讯批量 50 只/次快了一个数量级。更关键的是特色数据（涨停池、跌停池、炸板池、连板天梯、龙虎榜、热股榜、异动）它全都覆盖，而 v1.0 里这些我原本指望开盘了提供。
2. **历史 K 线仍以 easy-tdx 为主。** 同花顺 `/prices/historical` 接口**强约束单只标的、不接受逗号**，全市场要 5578 次请求；而 easy-tdx 已实测 28 请求/秒、全市场约 230 秒。这条结论没变。

一句话：**实时盘中快照靠腾讯/新浪/东财（高频、并发友好），全市场快照与特色数据靠同花顺（一次拿全量、覆盖广），历史K线靠 easy-tdx（批量快），开盘了做龙虎榜交叉校验。**

---

## 二、6 家数据源实测结论（全部验过，非推测）

### 2.1 总览

| 数据源 | 状态 | 角色定位 | 接入方式 |
|---|---|---|---|
| **同花顺** | ✅ **主力源** | 全市场快照、特色数据、财报、估值、复权因子、交易日历 | `https://fuyao.aicubes.cn` + `X-api-key` 头 |
| **东方财富** | ✅ 主干源 | 实时快照、日/周/月K、**复权因子**、板块 | `push2.eastmoney.com` + `push2his` |
| **腾讯** | ✅ 高频源 | 盘中快照（含五档盘口、换手率、量比） | `qt.gtimg.cn/q=`，GBK |
| **新浪** | ✅ 备用源 | 快照（含五档盘口） | `hq.sinajs.cn/list=`，需 Referer |
| **通达信** | ✅ **K线主力** | 日K/分钟K（复权） | NAS easy-tdx `NAS_INTERNAL_IP:8000` |
| **开盘了** | ✅ 补充源 | 龙虎榜（含游资席位、概念标签） | NAS kpl-proxy `NAS_INTERNAL_IP:8018` |

### 2.2 同花顺实测明细（本次最大收获）

BaseURL `https://fuyao.aicubes.cn`，鉴权 **Header `X-api-key`**（不是 `Authorization: Bearer`）。
响应是统一信封 `ApiResponse`，业务数据在 `data.item`。完整端点文档已下载到 `docs/hithink-llms-full.txt`（301KB，94 个端点）。

**✅ 已验证可用（code=0）**

| 端点 | 实测结果 | 备注 |
|---|---|---|
| `/api/a-share/prices/snapshot` | **1.38MB，全市场 5578 只** | ⭐ 一次拿全量，效率极高 |
| `/api/a-share/prices/historical` | 正常 | ⚠️ 单只标的、仅 `interval=1d` |
| `/api/a-share/special-data/limit-up-pool` | 35 只 | 涨停池 |
| `/api/a-share/special-data/limit-down-pool` | 正常 | 跌停池 |
| `/api/a-share/special-data/limit-break-pool` | 正常 | 炸板池 |
| `/api/a-share/special-data/limit-up-ladder` | 48KB | 连板天梯 |
| `/api/a-share/special-data/dragon-tiger-list` | 72 条/63 只 | 龙虎榜，含概念标签 |
| `/api/a-share/special-data/skyrocket-list` | 正常 | 飙升榜 |
| `/api/a-share/special-data/hot-stock-list` | 正常 | 热股榜 |
| `/api/a-share/special-data/anomaly-analysis-list` | 186KB | 异动分析 |
| `/api/a-share/valuations/snapshot` | PE/PB/PS/PCF | 估值，参数 `thscodes` |
| `/api/a-share/financials/income-statements` | 正常 | 利润表，需 `period=annual/quarterly` |
| `/api/a-share/financials/balance-sheets` | 正常 | 资产负债表 |
| `/api/a-share/financials/cash-flow-statements` | 正常 | 现金流量表 |
| `/api/a-share/corporate-actions/adjustment-factors` | 正常 | 复权因子 |
| `/api/a-share/calendar/trading-days` | 正常 | 交易日历 |
| `/api/a-share/auction/snapshot` | 正常 | 集合竞价，参数 `thscodes` |
| `/api/meta/tickers/search` \| `list` | 正常 | 代码检索，参数 `q` |
| `/api/a-share-index/*` | 正常 | 指数快照/历史/成分股 |

**❌ 权限受限（code=2004，此 Key 无权，需客户端版）**

| 端点 | 说明 |
|---|---|
| `/api/a-share/capital-flow/snapshot` \| `historical` | "该数据为同花顺AI客户端专用" |
| `/api/a-share/high-frequency/intraday` \| `historical` | 同上 |
| `/api/news/events/search` | 同上 |

> 💡 **应对**：资金流这块缺口用**东方财富资金流接口**补——东财本来就有，不影响整体能力。高频分钟数据用 easy-tdx 的分钟K补。

### 2.3 开盘了实测明细

你部署的 `kpl-proxy`（`NAS_INTERNAL_IP:8018`）把签名问题解决了——它自动注入 `DeviceID/PhoneOSNew/VerSion/apiv=w44`，并按 `c` 参数路由到三个上游（历史/今日行情/龙虎榜）。CORS 全开。

**✅ 已验证可用**

```
GET http://NAS_INTERNAL_IP:8018/w1/api/index.php?c=LongHuBang&a=GetStockList
→ 48KB，55 条龙虎榜，字段含：
  ID/Name/IncreaseAmount/BuyIn(净买入)/JoinNum(席位数)/
  Turnover(成交额)/CircPrice(流通市值)/Amplitude/TurnoverRatio
```

代理源码里 `FIXED_C` 表明还支持这些类：`HomeDingPan`(今日盯盘)、`HisHomeDingPan`、`HisLimitResumption`(涨停复盘)、`UserSelectStock`、`StockL2History`、`Stock`、`BusinessGroup`(游资席位)、`Index`(游资动向)。

> 注意：`c=HomeDingPan` 系列实测返回空（可能需要 `KPL_TOKEN` 环境变量）。**龙虎榜已确认可用**，其余接口后续按需补。

### 2.4 其余四源（v1.0 已验证，结论不变）

- **腾讯**：批量 50~60 只/次、并发 6 最稳。解析**必须 `re.finditer(r'v_(\w+)="([^"]*)"')`**，不能用 `re.match`。
- **新浪**：需带 `Referer: https://finance.sina.com.cn`。
- **东财**：`secid` = `1.600000`(沪) / `0.000001`(深)。复权因子是独家优势。
- **通达信**：easy-tdx `/api/v1/bars`，参数 `market`(SH/SZ/BJ) + `code`。服务端死限约 **28 请求/秒**，全市场约 **230 秒**。

---

## 三、系统架构（v2.0，数据源职责已重排）

```
   6 家数据源                    绿联 NAS (Docker Compose)
 ┌────────────────┐         ┌──────────────────────────────────┐
 │ 同花顺 fuyao   │ 全市场  │  ┌────────────────────────────┐  │
 │   (主力)       │ 快照    │  │ collector（独立容器）        │  │
 │ 腾讯/新浪/东财 │ 盘中    │  │  APScheduler 定时调度        │  │
 │   (高频)       │ 高频    │  │  ├ 盘中快照  腾讯/新浪       │  │
 │ easy-tdx       │ 历史K   ├─>│  ├ 全市场快照 同花顺(1次)   │  │
 │   (K线主力)    │         │  │  ├ 特色数据  同花顺         │  │
 │ kpl-proxy      │ 龙虎榜  │  │  ├ 全市场日K easy-tdx      │  │
 │   (补充校验)   │         │  │  └ 龙虎榜    kpl-proxy     │  │
 └────────────────┘         │  └──────────┬─────────────────┘  │
                            │             │ 写入                │
                            │   ┌─────────▼────────┐ ┌───────┐ │
                            │   │  PostgreSQL 16   │ │ Redis │ │
                            │   │ users/apikeys    │ │ 缓存  │ │
                            │   │ quotes/klines    │ │ 限流  │ │
                            │   │ (时间分区表)      │ │       │ │
                            │   └─────────┬────────┘ └───┬───┘ │
                            │             │              │     │
                            │   ┌─────────▼──────────────▼───┐ │
                            │   │   api（FastAPI 后端）        │ │
                            │   │  /api/v1/*  数据接口         │ │
                            │   │  /auth/*    注册登录         │ │
                            │   │  /apikey/*  密钥管理         │ │
                            │   │  /admin/*   后台             │ │
                            │   └─────────┬──────────────────┘ │
                            │   ┌─────────▼──────────────────┐ │
                            │   │  web（Next.js 前台+后台）    │ │
                            │   └────────────────────────────┘ │
                            │   ┌────────────────────────────┐ │
                            │   │  nginx（唯一对外入口）       │ │
                            │   └────────────────────────────┘ │
                            └──────────────────────────────────┘
```

### 容器清单（不变）

| 容器 | 职责 |
|---|---|
| `nginx` | 反向代理、静态资源，唯一对外入口 |
| `web` | Next.js 用户控制台 + API 文档 + 管理后台 |
| `api` | FastAPI 后端，无状态可扩容 |
| `collector` | 采集调度，**独立生命周期，不与 api 耦合** |
| `postgres` | 主库 |
| `redis` | 缓存 + 限流计数 |

> ⚠️ 绿联部署注意：编排文件必须 `docker-compose.yaml`（`.yml` 不认）；去掉 `env_file:`（.env 内联到 `environment:`）；`condition: service_healthy` 改 `service_started`；数据目录 `chown -R 999:999 + chmod 777`。

---

## 四、数据源抽象层（技术核心，v2.0 扩充）

```
Adaptor (抽象基类) —— 统一输出 Quote / KlineBar / SpecialData 模型
  ├─ HithinkAdaptor     → fuyao REST，JSON，X-api-key 头
  ├─ TencentAdaptor     → GBK + ~ 分隔，re.finditer 解析
  ├─ SinaAdaptor        → GBK + , 分隔，需 Referer
  ├─ EastmoneyAdaptor   → JSON，f43/f44 编码字段
  ├─ TdxAdaptor         → easy-tdx，结构化
  └─ KaipanlaAdaptor    → kpl-proxy，龙虎榜/情绪
```

统一输出模型（所有适配器必须产出）：

```python
class Quote(BaseModel):
    source: str          # 来源标识，用于交叉校验
    code: str            # 统一代码 600000.SH / 000001.SZ
    name: str
    price: float
    pre_close: float
    open: float; high: float; low: float
    volume: int          # 成交量（股）
    amount: float        # 成交额（元）
    change_pct: float
    turnover_rate: float | None   # 换手率 %
    vol_ratio: float | None       # 量比
    circ_mv: float | None         # 流通市值
    bid: list[tuple] | None       # 五档买盘
    ask: list[tuple] | None       # 五档卖盘
    ts: datetime
```

**这套抽象的三个价值：**
1. **主备自动切换** —— 某源挂了自动降级，接口层无感。
2. **交叉校验** —— 多源比对，偏差超阈值告警。数据质量是这类平台的生命线。
3. **新增源只写一个适配器文件**，不动上层代码。

---

## 五、功能模块

### 5.1 用户体系
邮箱 + 密码（argon2 哈希）；JWT（access 2h + refresh 7d）；等级 `free`/`pro`/`vip` 对应不同 QPS 与日调用上限。

### 5.2 APIKey 体系（对标同花顺那张截图）
| 功能 | 说明 |
|---|---|
| 创建 | 自助创建，可命名、设权限范围、设过期时间 |
| 展示 | **明文只显示一次**，之后只存 `sha256` 哈希；列表显示 `sk_live_xxxx****xxxx` |
| 吊销 | 即时生效（Redis 黑名单） |
| 轮换 | 一键刷新，旧 key 立刻失效 |
| 多 key | 单用户可建多个，便于分环境 |

调用：`Authorization: Bearer sk_live_xxxxxxxx`

### 5.3 对外 API
```
GET /api/v1/quote?codes=600000.SH,000001.SZ        # 实时快照（多源合并）
GET /api/v1/kline?code=600000.SH&period=day&limit=100&adjust=qfq
GET /api/v1/stock/list                             # 股票列表
GET /api/v1/trade/calendar                         # 交易日历
GET /api/v1/valuation?code=...                     # 估值（同花顺）
GET /api/v1/financials/{income|balance|cashflow}   # 财报（同花顺）
GET /api/v1/limit-up  /limit-down /limit-break     # 涨停/跌停/炸板池
GET /api/v1/limit-up-ladder                        # 连板天梯
GET /api/v1/dragon-tiger                           # 龙虎榜（同花顺+开盘了校验）
GET /api/v1/hot-stock /skyrocket /anomaly          # 热股/飙升/异动
GET /api/v1/usage                                  # 我的调用量
```

统一响应：
```json
{"code":0,"msg":"ok","data":{...},
 "meta":{"source":"hithink","cached":true,"ts":"2026-09-28T15:00:00+08:00"}}
```

### 5.4 限流与计量（Redis）
按 APIKey 滑动窗口计数；超额返回 `429` + `Retry-After`；调用日志异步批量落 `api_logs`。

### 5.5 后台管理
用户管理（封禁/改等级）、密钥管理（强制吊销）、数据源健康监控（成功率/延迟/限流次数）、调用统计图表、系统配置（源开关/QPS/缓存TTL）。

---

## 六、数据库设计（核心表）

```sql
CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    email         VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    tier          VARCHAR(16) DEFAULT 'free',
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE api_keys (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT REFERENCES users(id) ON DELETE CASCADE,
    name        VARCHAR(64),
    key_hash    VARCHAR(128) UNIQUE NOT NULL,  -- 只存哈希
    key_prefix  VARCHAR(16) NOT NULL,          -- 展示用 sk_live_xxxx
    scopes      TEXT[] DEFAULT '{quote,kline}',
    rate_limit  INT DEFAULT 60,                -- 次/分钟
    expires_at  TIMESTAMPTZ,
    is_active   BOOLEAN DEFAULT TRUE,
    last_used   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 行情快照（按月分区）
CREATE TABLE quotes (
    code          VARCHAR(16) NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    name          VARCHAR(32),
    price         NUMERIC(12,3), pre_close NUMERIC(12,3),
    open          NUMERIC(12,3), high     NUMERIC(12,3), low NUMERIC(12,3),
    volume        BIGINT,       amount    NUMERIC(20,2),
    change_pct    NUMERIC(8,3), turnover_rate NUMERIC(8,3),
    vol_ratio     NUMERIC(8,3), circ_mv NUMERIC(16,2),
    PRIMARY KEY (code, ts)
) PARTITION BY RANGE (ts);

-- K线
CREATE TABLE klines (
    code     VARCHAR(16) NOT NULL,
    period   VARCHAR(8)  NOT NULL,   -- day/week/month/60min
    adjust   VARCHAR(4)  NOT NULL,   -- none/qfq/hfq
    date     DATE        NOT NULL,
    open     NUMERIC(12,3), high NUMERIC(12,3),
    low      NUMERIC(12,3), close NUMERIC(12,3),
    volume   BIGINT,     amount NUMERIC(20,2),
    PRIMARY KEY (code, period, adjust, date)
);
CREATE INDEX idx_klines_date ON klines(date);

-- 特色数据（龙虎榜/涨停池等，半结构化，用 JSONB 存明细）
CREATE TABLE special_data (
    id       BIGSERIAL PRIMARY KEY,
    kind     VARCHAR(32) NOT NULL,   -- dragon_tiger / limit_up ...
    date     DATE NOT NULL,
    source   VARCHAR(16) NOT NULL,
    payload  JSONB NOT NULL,
    UNIQUE (kind, date, source)
);

-- 调用日志（按天分区，可 TTL 清理）
CREATE TABLE api_logs (
    id BIGSERIAL, key_id BIGINT, path VARCHAR(128),
    status INT, latency_ms INT, ts TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);
```

**设计要点**
- `api_keys` **只存哈希**——库泄露也不影响用户密钥安全。
- 时间序列表**分区**——行情数据增长极快，分区让查询剪枝、旧数据整块归档，不用 `DELETE` 扫全表。
- 唯一约束保证**幂等**——重跑采集用 `ON CONFLICT DO UPDATE`，不产生脏数据。
- 特色数据用 `JSONB`——各源字段差异大，JSONB 比建几十张窄表灵活得多。

---

## 七、采集器任务表（v2.0 重排）

| 任务 | 频率 | 数据源 | 说明 |
|---|---|---|---|
| 盘中实时快照 | 交易时段 3 秒 | 腾讯(主) → 新浪/东财(备) | 分批 50 只、并发 6 |
| **全市场快照** | 每 30 秒 | **同花顺** | ⭐ 1 次请求拿 5578 只，成本极低 |
| 特色数据 | 交易日 15:05 | 同花顺 | 涨停/跌停/炸板/连板/热股/异动 |
| 龙虎榜 | 交易日 18:00 | 同花顺 + 开盘了 | 双源交叉校验 |
| **全市场日K** | 每日 15:30 | **easy-tdx** | 并发 12，约 230 秒 |
| 复权因子 | 每日盘后 | 东方财富 | 计算前/后复权 |
| 财报/估值 | 每周 | 同花顺 | 财务、估值 |
| 股票列表 | 每日 | 同花顺 `meta/tickers/list` | 代码-名称映射 |
| 交易日历 | 每月 | 同花顺 | — |

**三条硬约束**
1. **限流保护**：每源独立令牌桶。腾讯 ≤60 只/批、easy-tdx ≤28 请求/秒——实测硬上限，超了直接被掐。同花顺不限制累计次数但会动态限流（`429`/`code=4001`），需退避。
2. **失败重试 + 降级**：指数退避 3 次，仍失败切备用源并告警。
3. **交易时段感知**：非交易时段停快照任务，避免无效请求触发风控。

---

## 八、Docker 部署

```yaml
# docker-compose.yaml（绿联必须用 .yaml 后缀）
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: stockdata
      POSTGRES_USER: stockdata
      POSTGRES_PASSWORD: <内联实际密码>       # 不用 env_file
    volumes:
      - ./data/postgres:/var/lib/postgresql/data
      - ./init:/docker-entrypoint-initdb.d
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes:
      - ./data/redis:/data
    restart: unless-stopped

  api:
    build: ./backend
    environment:
      DATABASE_URL: postgresql+psycopg://stockdata:<pwd>@postgres:5432/stockdata
      REDIS_URL: redis://redis:6379/0
      JWT_SECRET: <随机串>
      HITHINK_API_KEY: <内联>
    depends_on: [postgres, redis]
    restart: unless-stopped

  collector:
    build: ./collector
    environment:
      DATABASE_URL: postgresql+psycopg://stockdata:<pwd>@postgres:5432/stockdata
      REDIS_URL: redis://redis:6379/0
      HITHINK_API_KEY: <内联>
      EASYTDX_BASE: http://NAS_INTERNAL_IP:8000
      KPL_PROXY_BASE: http://NAS_INTERNAL_IP:8018
    depends_on: [postgres, redis]
    restart: unless-stopped

  web:
    build: ./frontend
    restart: unless-stopped

  nginx:
    image: nginx:alpine
    ports: ["9850:80"]
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on: [api, web]
    restart: unless-stopped
```

部署目录：`/volume1/docker/stockdata/`
容器间用服务名互访（`postgres:5432`）；访问 NAS 上已有服务用宿主 IP（`NAS_INTERNAL_IP:8000` / `:8018`）。

---

## 九、实施路线

### 一期：最小可用闭环
1. 项目骨架 + `docker-compose.yaml` + PG/Redis
2. 数据源抽象层 + **6 个适配器**
3. 用户注册登录 + JWT
4. APIKey 创建/吊销 + Redis 限流
5. 核心接口：`/quote`、`/kline`、`/stock/list`、`/limit-up`、`/dragon-tiger`
6. collector 采集调度（快照 + 日K + 特色数据）
7. 简易管理后台

### 二期：增强
8. 财报/估值/复权/交易日历接口
9. 多源交叉校验 + 数据源健康大盘
10. API 文档站 + 在线调试页（对标你截图）
11. 开盘了其余接口（盯盘/涨停复盘/游资席位）

### 三期：商业化
12. 套餐计费、用量配额
13. WebSocket 实时推送（如需）
14. 性能压测调优

---

## 十、风险与对策

| 风险 | 对策 |
|---|---|
| 数据源接口变更 | 适配器隔离 + 健康检查 + 主备切换 + 告警 |
| 免费源反爬升级 | 严守实测限流上限、UA 轮换、变化即告警；关键数据双源冗余 |
| 同花顺 Key 配额/权限 | 资金流/高频端点已受限（2004），**用东财资金流 + easy-tdx 分钟K 补**；关注额度 |
| 开盘了部分接口空响应 | 龙虎榜已可用；`HomeDingPan` 需 `KPL_TOKEN`，按需配置 |
| 绿联 UI 部署兼容性 | `.yaml` 后缀、去 `env_file`、`service_started` |
| **数据合规** | ⚠️ 见下节 |

---

## 十一、合规提示（重要）

⚠️ 把 6 家（尤其同花顺这类有明确知识产权的源）的数据聚合成 API 对外分发，**商业场景存在版权风险**。

- **自用/内部分析** → 无虞，放心做
- **对外开放** → 建议对数据做二次加工（计算衍生指标），对"原始行情转售"保持谨慎
- 建议先以"技术演示/研究用途"定位上线，商业化放到合规评估之后

另外：**你的 API Key 已存到 `.hithink_key` 并加入 `.gitignore`**，请勿提交到任何仓库。

---

## 附：本次实测产出物

| 文件 | 说明 |
|---|---|
| `docs/hithink-llms-full.txt` | 同花顺完整 API 文档（301KB，94 端点） |
| `.hithink_key` | 同花顺 API Key（已 gitignore） |
| `.gitignore` | 凭据与构建产物忽略规则 |

*v2.0 基于 2026-09-28 对 6 家数据源的完整实测，所有结论均为实测结果而非推测。*
