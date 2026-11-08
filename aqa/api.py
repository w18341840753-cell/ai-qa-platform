"""Webhook 服务（可选依赖 FastAPI）：接 GitHub push 事件后异步跑流水线。

启动：
    pip install fastapi uvicorn
    python -m aqa serve --port 8080

GitHub 侧配置：仓库 Settings → Webhooks → Payload URL 填 http(s)://<host>/webhook/github
把同一个 secret 写进 .env 的 AQA_WEBHOOK_SECRET，服务端会校验 X-Hub-Signature-256。

安全：不配 secret 时接口是开放的（任何人都能触发布流水线、烧模型额度），
因此**监听公网地址时必须配置 secret**，否则启动即告警。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import uuid
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


def webhook_secret() -> str:
    return (os.environ.get("AQA_WEBHOOK_SECRET") or "").strip()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """校验 GitHub 的 X-Hub-Signature-256（HMAC-SHA256）。

    用 compare_digest 做定长比较，避免逐字节短路带来的时序侧信道。
    """
    if not secret:
        return True  # 未配置 secret：保持旧行为，由启动告警提示风险
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _execute(run_key: str, payload: dict[str, Any]) -> None:
    try:
        settings = Settings.from_env()
        ctx, outcomes = Orchestrator(settings).run(
            event=payload,
            quiet=True,
        )
        with _LOCK:
            RUNS[run_key] = {
                "status": "done" if all(o.ok for o in outcomes) else "failed",
                "run_id": ctx.run_id,
                "artifacts": str(ctx.artifacts),
                "stages": [o.__dict__ for o in outcomes],
            }
    except Exception as exc:  # noqa: BLE001 - 后台任务必须兜住所有异常
        with _LOCK:
            RUNS[run_key] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


@app.on_event("startup")
def _warn_if_open() -> None:
    """监听公网却没配 secret —— 上线前最容易漏的一步，直接在日志里喊出来。"""
    if webhook_secret():
        return
    print(
        "[aqa][warn] 未配置 AQA_WEBHOOK_SECRET：/webhook/github 接受任意来源请求，"
        "任何人都能触发测试并消耗模型额度。公网部署请务必设置。",
        flush=True,
    )


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "signature_required": bool(webhook_secret())}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    background: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, str]:
    """验签 → 先返回 202 → 后台跑流水线（避免 GitHub 等待超时重复投递）。"""
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(status_code=413, detail="payload too large")

    if not verify_signature(webhook_secret(), body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    run_key = uuid.uuid4().hex[:8]
    with _LOCK:
        RUNS[run_key] = {"status": "queued"}
    background.add_task(_execute, run_key, payload)
    return {"accepted": run_key}


@app.get("/runs/{run_key}")
def get_run(run_key: str) -> dict[str, Any]:
    with _LOCK:
        return RUNS.get(run_key, {"status": "not_found"})


@app.get("/runs")
def list_runs() -> dict[str, Any]:
    with _LOCK:
        return {"count": len(RUNS), "runs": RUNS}
