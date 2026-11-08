"""运行上下文：一次流水线的全部输入、中间产物与审计日志。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def new_run_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


@dataclass
class RunContext:
    run_id: str
    repo: str
    commit: str
    branch: str = "main"
    device_id: str = "unknown"
    artifacts: Path = field(default_factory=Path)
    data: dict[str, Any] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.artifacts.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 产物落盘

    def save(self, name: str, payload: Any) -> Path:
        """把任意阶段结果写成 JSON，便于事后复盘与审计。"""
        path = self.artifacts / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return path

    def save_text(self, name: str, text: str) -> Path:
        path = self.artifacts / name
        path.write_text(text, encoding="utf-8")
        return path

    # ---------------------------------------------------------------- 审计日志

    def log(self, stage: str, status: str, message: str = "", **extra: Any) -> None:
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "stage": stage,
            "status": status,
            "message": message,
            "elapsed_s": round(time.time() - self.started_at, 2),
            **extra,
        }
        self.timeline.append(entry)
        with (self.artifacts / "timeline.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
