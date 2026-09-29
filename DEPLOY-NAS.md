# StockData 部署到绿联 NAS（先部署、支付后补版）

> **状态：已上线** ✅
> 地址 `http://NAS_INTERNAL_IP:9850/`，5 个容器全部 Up，自动验收 17/17 通过。
> 管理员 `admin@example.com`。

**结论：可以先部署。** 当前版本功能完整、回归 6/6 全绿，
唯一不能对外的是「真实收款」—— 已在部署配置里把模拟支付关掉，
所以现在上线是安全的：站点能完整跑通，只是收不到钱。

**日常重新部署**（改完代码后）：
```bash
python -u tools/upload.py && python -u tools/deploy.py 2 && python -u tools/acceptance.py
```

---

## 一、部署前必读：已经堵掉的口子

余额可以直接买套餐（VIP = 每分钟 1200 次、不限日调用量），
所以**任何"一点就到账"的充值通道都等于白送配额**。
原本有两条白嫖路径，都已堵掉：

| 路径 | 原状 | 现在 |
|---|---|---|
| `mock` 渠道自助 `/pay` | 登录用户点一下立刻到账 | `ALLOW_MOCK_PAY=false` 时 403 |
| `manual` 渠道自助 `/pay` | **绕过后台确认**直接入账 | 一律拒绝，必须后台确认 |

配置位置：
- `docker-compose.yaml` → `api.environment.ALLOW_MOCK_PAY: "false"` ← 已设好，**别改回 true**
- `backend/.env` → `ALLOW_MOCK_PAY=true`（只管本地测试，不会被打包进镜像）

用户现在能用的充值方式只剩 **线下转账**（下单 → 你后台确认到账），
功能完整，只是需要你手工点一下。

---

## 二、部署步骤

### 1. 在 NAS 上准备目录

```bash
ssh aeroxw@NAS_INTERNAL_IP
sudo mkdir -p /volume1/docker/stockdata/{data/postgres,data/redis,init}
cd /volume1/docker/stockdata

# 绿联镜像 UID 与宿主不对应，bind mount 必须这样改，否则 PG/redis 起不来
sudo chown -R 999:999 data/postgres data/redis
sudo chmod -R 777 data/postgres data/redis
```

> `init/` 是 PG 的初始化脚本目录，留空即可（表结构由 SQLAlchemy 自己建）。

### 2. 上传文件 —— 一条命令搞定

项目里已经备好脚本，**不用手工 scp**：

```bash
python -u tools/upload.py          # 传代码，自动转 LF、排除 .pyc/.env/.db
```

脚本内部处理了几个坑：
- **SFTP 被绿联禁用** → 用 `exec_command("cat > file")` + 写 stdin
- `.py` / `Dockerfile` / `.conf` 自动转 LF（Windows 的 CRLF 会让容器里直接崩）
- 自动排除 `__pycache__`、`backend/.env`、`backend/stockdata.db`

> ⚠️ 别传 `backend/.env`。容器内配置全部走 `docker-compose.yaml` 的
> `environment:`（绿联 UI 不认 `env_file:`）。
> 也别传 `backend/stockdata.db`（那是本地 SQLite，NAS 上用 PG）。

### 3. 确认 9850 没被占用

```bash
sudo netstat -tlnp | grep -E '9850|8000|8018'
```

- `8000` = easy-tdx、`8018` = kpl-proxy，**都别动**
- `9850` 应该是空的；如果被占，改 `docker-compose.yaml` 的 `ports`

### 4. 构建并启动

```bash
python -u tools/deploy.py 1     # 先起 postgres / redis（秒级）
python -u tools/deploy.py 2     # 再构建 api / collector / nginx（约 1~2 分钟）
```

或者一条到底：`python -u tools/deploy.py all`

> 首次构建实测 **约 90 秒**（两个 Python 镜像并行编），比预想快很多。

<details>
<summary>也可以用绿联 Container Manager UI（点这里展开）</summary>

用「项目/Compose」方式，指向 `/volume1/docker/stockdata/docker-compose.yaml`。

已在文件里规避过的绿联坑（**不要改回去**）：
- 文件名必须是 `.yaml`（`.yml` 绿联 UI 不认）
- 不用 `env_file:`，全部内联到 `environment:`
- 不用 `condition: service_healthy`（用默认的启动顺序即可）
- 数据目录用 `./xxx:/...` 相对路径

</details>

### 5. 验收

```bash
python -u tools/acceptance.py      # 17 项自动验收
```

### 6. 首访：注册管理员

浏览器打开 `http://NAS_INTERNAL_IP:9850/`

**第一个注册的账号自动成为管理员**（`is_first → is_admin=True`）。
所以注册完立刻就是后台管理员，别再注册第二个然后再想着升级。

---

## 三、验收清单

| 项 | 预期 |
|---|---|
| `http://NAS_INTERNAL_IP:9850/` | 首页正常 |
| 注册 + 登录 | 通过，进控制台 |
| 控制台「用量统计」 | 图表正常，数字清晰 |
| 生成 API Key | 免费版最多 3 个 |
| 用 Key 调 `/api/v1/quote?codes=600519.SH` | 返回行情 |
| 充值下单 | **只剩「线下转账」**，没有"立即支付"按钮 |
| 后台 `/admin.html` | 能看到用户/密钥/日志，缓存状态应为**已连接**（容器内 redis） |
| 后台改用户级别、指派套餐 | 生效 |

---

## 三·五、改完代码怎么上线（最容易踩的一坑）

**`backend/` 是 `COPY` 进镜像的，没有 bind mount。**

所以改了 Python 代码之后：

| 做法 | 结果 |
|---|---|
| `docker restart stockdata-api` | ❌ **无效**，跑的还是旧镜像里的代码 |
| `docker compose up -d --build api` | ✅ 生效 |

标准流程（本机一条命令）：

```bash
python tools/upload.py      # 传代码到 /volume1/docker/stockdata
python tools/deploy.py 2    # 重建 api/collector 镜像并重启 + 重启 nginx
python tools/healthcheck.py # 46 项体检
```

`deploy.py 2` 里**必须重启 nginx**：nginx 会缓存 upstream 的 DNS 解析，
api 容器 recreate 后 IP 变了它还指着旧 IP → 全站 502，
但 `docker ps` 显示容器全 Up，极具迷惑性。

---

## 四、已知待办（不影响上线）

1. **支付商户参数** —— 网关、验签、回调、对账**全部已实现**，只差商户参数。
   拿到资质后填 `docker-compose.yaml` 里 `WXPAY_*` / `ALIPAY_*` 和
   `SITE_BASE_URL`，重启 api 即可，前端会自动出现扫码入口（详见第七节）。
   退款接口尚未实现。
2. **多 worker 缓存不共享** —— Dockerfile 用 `--workers 2`，
   `plans.py` 的套餐/用量缓存是进程内的，两个 worker 各算一份，
   30 秒 TTL 内会漏算另一个 worker 的调用量（**配额偏松，不会误伤**）。
   要精确得把缓存挪到 Redis。
   同理，后台「数据源监控」的 ok/fail 计数是**进程内**的，
   看到某源 `ok=0 fail=0` 只说明**这个 worker 本轮没调到它**，不等于故障。
3. **PG 就绪竞态** —— `api` 只等 `postgres` 启动、不等它 ready，
   极端情况下首批请求会 500，`restart: unless-stopped` 会自愈。
4. 部署后建议**再改一次 `POSTGRES_PASSWORD`**（现在是 `stockdata_2026`），
   同步改 `docker-compose.yaml` 里两处 `DATABASE_URL`。
5. **东财被限流时的表现** —— 详见下节。它排在快照链最后一位，
   触发限流时会自动下沉，不影响取数，只是后台会显示该源 fail 增加。

---

## 四·五、东方财富的限流（排查结论，别再踩一遍）

**症状**：`RemoteProtocolError: Server disconnected without sending a response.`
——注意它**不返回 429**，而是在 TCP 层直接掐断连接，靠状态码完全判断不出来。

**排查过程（结论比猜测靠谱）**：

1. 先怀疑 IPv6：宿主上 `curl -6` 确实 0/5、`-4` 是 7/8，
   但**容器里裸 httpx 是 8/8 成功** → 排除。
2. 再怀疑 keep-alive 死连接复用（特征"第 1 次成功、之后连挂"完全吻合），
   于是让东财每次用全新连接 → **故障依旧** → 排除。
3. 最后控制变量只改请求间隔，真相才出来：

| 请求间隔 | 成功率 |
|---|---|
| 0.2s | 0/8 |
| 1s | 0/8 |
| 3s | 1/6 |
| **静默 20s 后** | **4/5** |

即：**配额用完就掐连接，必须静默一段才恢复，短时间内重试多少次都没用。**

**对策**（已写进 `eastmoney.py`）：
`min_interval=0.6`（节流，别把配额一次打光）、`concurrency` 8→2、
`retry_backoff=2.0`、`circuit_fail_threshold=3 / circuit_cooldown=30`
（连续失败 3 次就冷却 30s 并**快速失败**，别让用户干等 15s 超时 × 重试）。

改完后实测连续 13 次全成、`fail=0`。

> 通用能力做在 `base.py` 的 `BaseAdaptor` 上：`net_retries` / `retry_backoff`
> （5xx 和连接断开才重试，4xx 不重试）、`min_interval`、`circuit_*`。
> 回归测试：`python tools/test_adaptor_retry.py`（4 个场景）。

---

## 六、新用户试用期（15 天）

注册即送 **15 天专业版**体验，到期后自动停止调用数据接口（HTTP 402），
购买套餐后恢复。管理员账号**豁免**拦截（否则哪天自己账号过期，连后台都进不去）。

可调参数在 `docker-compose.yaml` 的 api 环境里：

| 变量 | 默认 | 说明 |
|---|---|---|
| `TRIAL_ENABLED` | true | 关掉则新用户直接是免费版 |
| `TRIAL_DAYS` | 15 | 试用天数 |
| `TRIAL_PLAN` | pro | 试用期间享受哪个套餐的配额 |
| `TRIAL_EXPIRE_BLOCK` | true | false = 到期只提示、不拦截 |

到期判断统一走 `plans.entitlement()`（唯一真源），前后台和鉴权链路读同一份，
不会出现"控制台显示还有 3 天、API 却已经 402"。

后台「用户管理」每行都标了**试用/付费 · 剩 N 天**，并有「有效期」按钮可手动顺延
（线下转账收款后在这里加天数）。

## 七、接入支付要做什么

代码已全部就位（`app/paygate/` + `routers/pay.py`），只差商户参数：

1. 微信商户平台拿：`mchid`、证书序列号、APIv3 密钥、商户私钥、微信支付公钥
2. 支付宝开放平台拿：`AppID`、应用私钥、支付宝公钥（应用公钥上传后换回）
3. 填进 `docker-compose.yaml` 的 `WXPAY_*` / `ALIPAY_*`，以及 `SITE_BASE_URL`
4. `python tools/deploy.py 2` 重启

**回调地址**（填到商户平台）：

```
https://<你的域名>/api/v1/pay/notify/wechat
https://<你的域名>/api/v1/pay/notify/alipay
```

未配置时前端只显示「线下转账」，不会出现点了必然报错的按钮。

安全上已经做掉的：验签（先验签再解密）、金额核对（对不上不入账）、
幂等（重复回调只入账一次）、应答格式区分（微信 JSON / 支付宝纯文本，
回错渠道会无限重发）。

## 八、回滚

```bash
cd /volume1/docker/stockdata
sudo docker compose down          # 停容器，数据卷保留
sudo docker compose down -v       # 连数据一起清（慎用）
```
