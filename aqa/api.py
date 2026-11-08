"""Webhook 服务（可选依赖 FastAPI）：接 GitHub push 事件后异步跑流水线。

启动：
    pip install fastapi uvicorn
    python -m aqa serve --port 8080

GitHub 侧配置：仓库 Settings → Webhooks → Payload URL 填 http://<host>:8080/webhook/github
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

try:  # pragma: no cover - 可选依赖
    from fastapi import BackgroundTasks, FastAPI
except ImportError as exc:  # pragma: no cover
    raise SystemExit("需要先安装：pip install fastapi uvicorn") from exc

from .config import Settings
from .orchestrator import Orchestrator

app = FastAPI(title="AI-Native QA Platform", version="0.1.0")

# 演示用内存态记录；生产应换 Redis / Postgres
RUNS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


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


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/github")
def github_webhook(payload: dict[str, Any], background: BackgroundTasks) -> dict[str, str]:
    """永远先返回 202，避免 GitHub 等待超时而重复投递。"""
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
