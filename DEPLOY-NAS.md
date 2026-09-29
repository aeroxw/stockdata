# StockData 部署到绿联 NAS

> **状态：已上线** ✅
> 地址 `http://NAS_INTERNAL_IP:9850/`，5 个容器全部 Up。
> 管理员 `admin@example.com`。
>
> **开源版说明**：本项目已移除全部支付与售卖功能（`paygate/`、订单、余额已删）。
> 额度一律由管理员在后台「档位管理」指派，不存在"充钱换配额"的通路。

**日常重新部署**（改完代码后）：
```bash
python -u tools/upload.py && python -u tools/deploy.py 2 && python -u tools/healthcheck.py
```

---

## 一、部署步骤

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

### 2. 首次部署前：填好环境变量

```bash
cp .env.example .env       # 项目根目录，docker compose 会自动加载
```

至少改这两项：

| 变量 | 生成方式 |
|---|---|
| `POSTGRES_PASSWORD` | 自己定一个强密码 |
| `JWT_SECRET` | `openssl rand -base64 48` |

`EASYTDX_BASE` / `KPL_PROXY_BASE` 指向你自己搭的服务（本项目不含），
没搭就留 localhost，对应数据源会自动降级。

> ⚠️ 用**绿联 Container Manager UI** 时 UI 不会读 `.env`，
> 那些 `${VAR:-默认值}` 会落到默认值上 —— 必须把 compose 里
> `environment:` 的值手改成真实值（密码尤其别留 `change_me`）。

### 3. 上传文件 —— 一条命令搞定

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

### 4. 确认 9850 没被占用

```bash
sudo netstat -tlnp | grep -E '9850|8000|8018'
```

- `8000` = easy-tdx、`8018` = kpl-proxy，**都别动**
- `9850` 应该是空的；如果被占，改 `docker-compose.yaml` 的 `ports`

### 5. 构建并启动

```bash
python -u tools/deploy.py 1     # 先起 postgres / redis（秒级）
python -u tools/deploy.py 2     # 再构建 api / collector / nginx（约 1~2 分钟）
```

或者一条到底：`python -u tools/deploy.py all`

> 首次构建实测 **约 90 秒**（两个 Python 镜像并行编）。

<details>
<summary>也可以用绿联 Container Manager UI（点这里展开）</summary>

用「项目/Compose」方式，指向 `/volume1/docker/stockdata/docker-compose.yaml`。

已在文件里规避过的绿联坑（**不要改回去**）：
- 文件名必须是 `.yaml`（`.yml` 绿联 UI 不认）
- 不用 `env_file:`，全部内联到 `environment:`
- 不用 `condition: service_healthy`（用默认的启动顺序即可）
- 数据目录用 `./xxx:/...` 相对路径

</details>

### 6. 验收

```bash
python -u tools/healthcheck.py     # 接口体检
python -u tools/acceptance.py      # 端到端验收
```

### 7. 首访：注册管理员

浏览器打开 `http://NAS_INTERNAL_IP:9850/`

**第一个注册的账号自动成为管理员**（`is_first → is_admin=True`）。
新用户一律落到免费版 `free`，额度不够就在后台给他换档。

---

## 二、验收清单

| 项 | 预期 |
|---|---|
| `http://NAS_INTERNAL_IP:9850/` | 首页正常，底部是「开源与自建」，**没有定价区** |
| 注册 + 登录 | 通过，进控制台 |
| 控制台 | 四个视图：概览 / API 密钥 / 我的配额 / 用量统计 |
| 控制台「我的配额」 | 显示三档额度，当前档位有标记，**无价格** |
| 生成 API Key | 免费版最多 3 个 |
| 用 Key 调 `/api/v1/quote?codes=600519.SH` | 返回行情 |
| 后台 `/admin.html` | 用户 / 密钥 / 日志 / 档位管理；缓存状态应为**已连接** |
| 后台用户行 | 只有「档位」下拉，**没有余额、有效期按钮** |
| 后台改用户档位 | 立即生效 |

---

## 三、改完代码怎么上线（最容易踩的一坑）

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
python tools/healthcheck.py # 接口体检
```

`deploy.py 2` 里**必须重启 nginx**：nginx 会缓存 upstream 的 DNS 解析，
api 容器 recreate 后 IP 变了它还指着旧 IP → 全站 502，
但 `docker ps` 显示容器全 Up，极具迷惑性。

---

## 四、已知待办 / 注意事项

1. **多 worker 缓存不共享** —— Dockerfile 用 `--workers 2`，
   `plans.py` 的档位/用量缓存是进程内的，两个 worker 各算一份，
   30 秒 TTL 内会漏算另一个 worker 的调用量（**配额偏松，不会误伤**）。
   要精确得把缓存挪到 Redis。
   同理，后台「数据源监控」的 ok/fail 计数是**进程内**的，
   看到某源 `ok=0 fail=0` 只说明**这个 worker 本轮没调到它**，不等于故障。
2. **PG 就绪竞态** —— `api` 只等 `postgres` 启动、不等它 ready，
   极端情况下首批请求会 500，`restart: unless-stopped` 会自愈。
3. **东财被限流时的表现** —— 详见下节。它排在快照链最后一位，
   触发限流时会自动下沉，不影响取数，只是后台会显示该源 fail 增加。
4. **密钥轮换** —— `JWT_SECRET` 与 `HITHINK_API_KEY` 曾经明文写在
   `docker-compose.yaml` 里（那个版本已进入 git 历史）。现在已改为
   环境变量占位，但**历史值仍建议轮换一次**。

---

## 五、东方财富的限流（排查结论，别再踩一遍）

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

## 六、回滚

```bash
cd /volume1/docker/stockdata
sudo docker compose down          # 停容器，数据卷保留
sudo docker compose down -v       # 连数据一起清（慎用）
```

---

## 七、给别人部署时要注意

- 仓库里的 `docker-compose.yaml` 带了一批**作者自用环境的默认值**
  （内网 `10.31.0.x` 的两个自建数据源地址）。换机器请通过 `.env` 覆盖。
- `docs/hithink-llms-full.txt`（同花顺官方文档全文）已排除在仓库外，
  需要时从 <https://fuyao.aicubes.cn/llms.txt> 自行获取。
- 对外提供服务前请自行确认上游数据条款与所在地合规要求 —— 详见 `NOTICE`。
