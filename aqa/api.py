"""Webhook 服务（可选依赖 FastAPI）：接 GitHub push 事件后异步跑流水线。

启动：
    pip install fastapi uvicorn
    python -m aqa serve --port 8080

GitHub 侧配置：仓库 Settings → Webhooks → Payload URL 填 http(s)://<host>/webhook/github
把同一个 secret 写进 .env 的 AQA_WEBHOOK_SECRET，服务端会校验 X-Hub-Signature-256。

安全：默认 fail-closed —— 不配 secret 时 /webhook/github 直接返回 401；
本地演示需要显式设置 AQA_ALLOW_OPEN_WEBHOOK=1 才会放行。即便配了 secret，
也建议同时设置 AQA_API_TOKEN 保护 /runs 读接口，并保持默认只监听 127.0.0.1。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any

try:  # pragma: no cover - 可选依赖
    from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
except ImportError as exc:  # pragma: no cover
    raise SystemExit("需要先安装：pip install fastapi uvicorn") from exc

from .config import Settings
from .orchestrator import Orchestrator

app = FastAPI(title="AI-Native QA Platform", version="0.2.0")

# 演示用内存态记录；生产应换 Redis / Postgres
RUNS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()

# 推送体上限：GitHub 的 push payload 正常在 100KB 量级，超过此值直接拒绝
MAX_BODY = 2 * 1024 * 1024
# 内存态运行记录上限，避免长期运行无限增长
MAX_RUNS = 200
# 投递去重：GitHub 重投时 X-GitHub-Delivery 不变，10 分钟内视为重复
MAX_DELIVERIES = 2048
DELIVERY_TTL_S = 600

_RATE: dict[str, list[float]] = {}
_DELIVERIES: "OrderedDict[str, float]" = OrderedDict()


def _flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def webhook_secret() -> str:
    return (os.environ.get("AQA_WEBHOOK_SECRET") or "").strip()


def allow_open_webhook() -> bool:
    """显式开放 webhook（仅用于本地演示）；默认 fail-closed。"""
    return _flag("AQA_ALLOW_OPEN_WEBHOOK")


def api_token() -> str:
    """读接口令牌；配置后 /runs 必须带 X-AQA-Token。"""
    return (os.environ.get("AQA_API_TOKEN") or "").strip()


def rate_limit_per_min() -> int:
    try:
        return int(os.environ.get("AQA_RATE_LIMIT_PER_MIN", "20"))
    except ValueError:
        return 20


def verify_signature(
    secret: str, body: bytes, header: str | None, *, allow_open: bool = False
) -> bool:
    """校验 GitHub 的 X-Hub-Signature-256（HMAC-SHA256）。

    用 compare_digest 做定长比较，避免逐字节短路带来的时序侧信道。
    未配置 secret 时**默认拒绝**；只有显式设置 AQA_ALLOW_OPEN_WEBHOOK=1 才放行。
    """
    if not secret:
        return allow_open
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _rate_limited(key: str) -> bool:
    """按来源做每分钟触发次数限制；0 表示关闭。"""
    limit = rate_limit_per_min()
    if limit <= 0:
        return False
    now = time.time()
    with _LOCK:
        bucket = [ts for ts in _RATE.get(key, ()) if now - ts < 60]
        if len(bucket) >= limit:
            _RATE[key] = bucket
            return True
        bucket.append(now)
        _RATE[key] = bucket
        if len(_RATE) > 1024:  # 防来源键无限增长
            _RATE.clear()
            _RATE[key] = bucket
    return False


def _duplicate_delivery(delivery_id: str | None) -> bool:
    """同一 X-GitHub-Delivery 在 TTL 内只处理一次，挡住重放。"""
    if not delivery_id:
        return False
    now = time.time()
    with _LOCK:
        while _DELIVERIES:
            oldest_id, ts = next(iter(_DELIVERIES.items()))
            if now - ts <= DELIVERY_TTL_S:
                break
            _DELIVERIES.popitem(last=False)
        if delivery_id in _DELIVERIES:
            return True
        _DELIVERIES[delivery_id] = now
        while len(_DELIVERIES) > MAX_DELIVERIES:
            _DELIVERIES.popitem(last=False)
    return False


def _store_run(run_key: str, payload: dict[str, Any]) -> None:
    with _LOCK:
        RUNS[run_key] = payload
        while len(RUNS) > MAX_RUNS:
            RUNS.pop(next(iter(RUNS)))


def _read_authorized(request: Request) -> bool:
    token = api_token()
    if not token:
        return True  # 未配置令牌时保持兼容，由启动告警提示风险
    return hmac.compare_digest(token, request.headers.get("x-aqa-token", ""))


def _execute(run_key: str, payload: dict[str, Any]) -> None:
    try:
        settings = Settings.from_env()
        ctx, outcomes = Orchestrator(settings).run(
            event=payload,
            quiet=True,
        )
        _store_run(
            run_key,
            {
                "status": "done" if all(o.ok for o in outcomes) else "failed",
                "run_id": ctx.run_id,
                "artifacts": str(ctx.artifacts),
                "stages": [o.__dict__ for o in outcomes],
            },
        )
    except Exception as exc:  # noqa: BLE001 - 后台任务必须兜住所有异常
        _store_run(run_key, {"status": "error", "error": f"{type(exc).__name__}: {exc}"})


@app.on_event("startup")
def _warn_if_open() -> None:
    """把「上线前最容易漏的一步」直接喊在日志里。"""
    if not webhook_secret():
        if allow_open_webhook():
            print(
                "[aqa][warn] AQA_ALLOW_OPEN_WEBHOOK=1：/webhook/github 接受任意来源请求，"
                "任何人都能触发测试并消耗模型额度。仅限本地演示，公网部署必须关掉。",
                flush=True,
            )
        else:
            print(
                "[aqa][error] 未配置 AQA_WEBHOOK_SECRET：/webhook/github 将拒绝所有请求。"
                "请在 .env 里设置与 GitHub Webhook Secret 一致的值。",
                flush=True,
            )
    if not api_token():
        print(
            "[aqa][warn] 未配置 AQA_API_TOKEN：/runs 与 /runs/{key} 对任何能访问服务的人可读，"
            "公网部署建议设置后带 X-AQA-Token 访问。",
            flush=True,
        )


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "signature_required": bool(webhook_secret()),
        "open_webhook": bool(not webhook_secret() and allow_open_webhook()),
        "runs_auth_required": bool(api_token()),
    }


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    background: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> dict[str, Any]:
    """验签 → 去重 → 限流 → 先返回 202 → 后台跑流水线。"""
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(status_code=413, detail="payload too large")

    client = request.client.host if request.client else "unknown"
    if _rate_limited(client):
        raise HTTPException(status_code=429, detail="too many requests")

    if not verify_signature(
        webhook_secret(), body, x_hub_signature_256, allow_open=allow_open_webhook()
    ):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    if _duplicate_delivery(x_github_delivery):
        return {"duplicate": True, "delivery": x_github_delivery}

    run_key = uuid.uuid4().hex[:8]
    _store_run(run_key, {"status": "queued"})
    background.add_task(_execute, run_key, payload)
    return {"accepted": run_key}


@app.get("/runs/{run_key}")
def get_run(run_key: str, request: Request) -> dict[str, Any]:
    if not _read_authorized(request):
        raise HTTPException(status_code=401, detail="invalid token")
    with _LOCK:
        return RUNS.get(run_key, {"status": "not_found"})


@app.get("/runs")
def list_runs(request: Request) -> dict[str, Any]:
    if not _read_authorized(request):
        raise HTTPException(status_code=401, detail="invalid token")
    with _LOCK:
        return {"count": len(RUNS), "runs": RUNS}
