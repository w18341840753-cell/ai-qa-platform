# 部署指南

先分清要部署的是什么 —— 这一步选错会白花钱：

| 要公开的东西 | 是什么 | 要不要买服务器 |
|---|---|---|
| `docs/index.html`（运行回放报告） | 一个静态文件 | **不要**。GitHub Pages / 对象存储就够，免费且自带 CDN |
| `aqa serve`（webhook 服务） | 常驻进程，接 GitHub push 回调、跑流水线 | 要。这是唯一需要服务器的部分 |

---

## 一、静态报告：GitHub Pages（推荐，免费）

本仓库已经配好，推 `main` 就自动发布：

```
https://w18341840753-cell.github.io/ai-qa-platform/
```

做法（新仓库可复用）：

1. 把 `tools/make_report.py` 生成的 HTML 放到 `docs/index.html`
2. 仓库 Settings → Pages → Source 选 `Deploy from a branch`，Branch 选 `main`，目录选 `/docs`
3. 等 30 秒，`https://<用户名>.github.io/<仓库名>/` 即可访问

命令行等价操作：

```bash
gh api -X POST repos/<owner>/<repo>/pages \
  --input - <<'EOF'
{"source":{"branch":"main","path":"/docs"}}
EOF
gh api -X PATCH repos/<owner>/<repo> -f homepage="https://<owner>.github.io/<repo>/"
```

> 局限：Pages 只托管静态文件，仓库得是 public（私有库要 GitHub Pro）。
> 不适合放内部报告 —— 那种情况用对象存储 + 签名 URL。

---

## 二、Webhook 服务：云服务器（Docker 一把梭）

### 1. 买机器

够用就行，这个服务没有重计算，瓶颈在模型 API 而不是 CPU：

| 选择 | 配置 | 参考价（2026-11 官网口径） |
|---|---|---|
| 腾讯云轻量 入门型 | 2 核 2G / 40–50G SSD / 3–4M | 新用户活动约 ¥99/15 个月；标价 ¥45–50/月，年付约 ¥459–510 |
| 腾讯云轻量 锐驰型 | 2 核 2G / 200M 峰值不限流量 | ¥45/月，年付 ¥459 |
| 阿里云同类（轻量 / ECS 经济型） | 2 核 2G | 价位相近，以活动页为准 |
| 学生认证 | 2 核 2G | 有专门优惠，需在活动页确认资格 |

> 价格波动大，下单前请以购买页实时价格为准。**2 核 2G 完全够用** ——
> 这个服务的负载是「收 webhook + 起子进程跑测试」，真正耗时的是等模型返回。

镜像选 **Ubuntu 22.04**。安全组（防火墙）只放 **22 / 80 / 443**，**不要**放 8000。

### 2. 装 Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # 重新登录生效
```

国内机器拉不到镜像时，配一下加速器（`/etc/docker/daemon.json`）：

```json
{ "registry-mirrors": ["https://docker.m.daocloud.io"] }
```

### 3. 拉代码 + 配密钥

```bash
git clone https://github.com/w18341840753-cell/ai-qa-platform.git
cd ai-qa-platform

cp .env.example .env
# 编辑 .env：填模型 Key、webhook secret
```

`.env` 最少要有这几行：

```ini
AQA_MODEL_MODE=api
AQA_TEXT_API_KEY=sk-...
AQA_VISION_API_KEY=sk-...          # 用同一个 DeepSeek Key 时可不填，会自动复用
AQA_BUILD_KIND=package
AQA_DEVICE_KIND=sim
AQA_SOURCE_DIR=/app/checkout
AQA_WEBHOOK_SECRET=<自己生成一串随机值>
AQA_REQUIRE_HUMAN_CONFIRM=false
```

生成 secret：

```bash
openssl rand -hex 32
```

### 4. 起服务

```bash
mkdir -p checkout runs .aqa
# 被测试的代码放在 checkout/ 里（示例用仓库自带的 demo 固件）
cp -r examples/firmware-demo/* checkout/

docker compose up -d --build
docker compose logs -f --tail=50
```

看到 `Uvicorn running on http://0.0.0.0:8000` 就成功了。容器内自检：

```bash
curl localhost:8000/healthz
# {"status":"ok","signature_required":true}
```

### 5. 域名 + HTTPS

```bash
sudo apt install -y nginx
sudo cp deploy/nginx.conf /etc/nginx/sites-available/aqa
sudo sed -i 's/qa.example.com/你的域名/' /etc/nginx/sites-available/aqa
sudo ln -s /etc/nginx/sites-available/aqa /etc/nginx/sites-enabled/aqa
sudo nginx -t && sudo nginx -s reload

sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d 你的域名    # 自动签证书 + 配 301 跳转
```

没有域名也可以先跑：服务器安全组放行 80，直接用 `http://<公网IP>` 回调。
GitHub 的 webhook **不要求 HTTPS**，但强烈建议上 —— 否则签名和 payload 都是明文。

### 6. 配 GitHub webhook

仓库 → Settings → Webhooks → Add webhook：

| 字段 | 值 |
|---|---|
| Payload URL | `https://你的域名/webhook/github` |
| Content type | `application/json` |
| Secret | 与 `.env` 里 `AQA_WEBHOOK_SECRET` **完全一致** |
| Which events | Just the push event |

保存后 GitHub 会立刻发一个 ping，服务端返回 202 就通了。回看某次运行：

```bash
curl https://你的域名/runs
curl https://你的域名/runs/<run_key>
```

---

## 三、上线前必须知道的三件事

**1. 不配 `AQA_WEBHOOK_SECRET` 等于把接口敞开**

`/webhook/github` 不验签时，任何人只要知道地址就能反复触发流水线，每跑一次烧一次模型额度。
服务端已实现 `X-Hub-Signature-256` 的 HMAC 校验（`aqa/api.py`），配了 secret 后伪造签名返回 401；
没配时启动日志会打印警告，`/healthz` 也会返回 `signature_required: false`。

**2. 必须配 `AQA_SOURCE_DIR`**

webhook 事件只告诉平台「哪个仓库、哪个 commit」，代码得自己 checkout 到 `checkout/`。
不配这一项，构建会拿容器内的工作目录当源码 —— 实测会把 3.9GB 的 `runs/` 压成 1.7GB 固件并卡死。
现在构建已经会跳过 `runs/ .git .aqa node_modules` 等目录，并设了 50MB 硬上限（`AQA_MAX_ARTIFACT_MB`），
超限直接报错而不是把磁盘写满。

**3. 运行记录在内存里**

`/runs` 的状态存在进程内存（`RUNS` dict），重启即丢。真正的证据是磁盘上的 `runs/<run_id>/`，
已通过 volume 持久化。要多实例/长期留档，把 `RUNS` 换成 Redis 或直接查 `runs/` 目录。

---

## 四、不想买服务器？还有两条路

| 方案 | 适合 | 代价 |
|---|---|---|
| GitHub Actions 定时跑 `python -m aqa run` | 只要「提交即回归」，不需要常驻 HTTP | 免费额度 2000 分钟/月；单次最多 6 小时；无公网 webhook |
| 云函数 / 容器实例（如腾讯云 CloudBase、阿里云 FC） | 低频触发，想按量付费 | 冷启动 + 需要改造成 handler；执行时长有上限 |

两者都不需要你维护进程和系统补丁，是这个平台更轻的落地方式。
