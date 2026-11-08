"""缺陷跟踪适配器：把 AI 生成的缺陷报告落到真实的 issue 系统。

设计要点 —— 去重指纹：
  同一个根因在不同 commit 上反复失败，不应该每次都建一遍 issue。
  指纹 = 用例 ID + 根因摘要，命中已有指纹则只追加评论（或直接跳过）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


class BugTracker:
    kind = "base"

    def __init__(self, store: Path) -> None:
        self.store = store
        self.store.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- 指纹与去重

    @staticmethod
    def fingerprint(root_cause: str) -> str:
        """按「根因」去重，而不是按用例。

        同一个根因会命中多个用例（例如配网缺陷同时导致不上线和超时重试），
        按用例去重会重复建 issue，污染缺陷库。这里只对根因文本做归一化后取哈希。
        """
        normalized = "".join(root_cause.split()).lower()
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]

    def _load(self) -> dict[str, Any]:
        if self.store.exists():
            try:
                return json.loads(self.store.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _save(self, data: dict[str, Any]) -> None:
        self.store.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def find_existing(self, fp: str) -> dict[str, Any] | None:
        return self._load().get(fp)

    def record(self, fp: str, payload: dict[str, Any]) -> None:
        data = self._load()
        data[fp] = payload
        self._save(data)

    # ------------------------------------------------------------- 接口

    def create(self, bug: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def confirm(self, bug_id: str, accepted: bool, note: str = "") -> dict[str, Any]:
        raise NotImplementedError


class LocalBugTracker(BugTracker):
    """演示 / 内网自建：缺陷写到本地 JSON，等价于「开发者已确认」的回写。"""

    kind = "local"

    def create(self, bug: dict[str, Any]) -> dict[str, Any]:
        fp = bug["fingerprint"]
        seq = len(self._load()) + 1
        bug_id = f"LOCAL-{seq:03d}"
        payload = {**bug, "id": bug_id, "url": f"local://bugs/{bug_id}", "status": "open"}
        self.record(fp, payload)
        return payload

    def confirm(self, bug_id: str, accepted: bool, note: str = "") -> dict[str, Any]:
        data = self._load()
        for fp, bug in data.items():
            if bug.get("id") == bug_id:
                bug["status"] = "confirmed" if accepted else "rejected"
                bug["confirm_note"] = note
                self._save(data)
                return bug
        raise KeyError(f"未找到缺陷 {bug_id}")


class GithubBugTracker(BugTracker):
    """真实闭环：用 gh CLI 建 issue（已登录状态下无需额外配 token）。"""

    kind = "github"

    def __init__(self, store: Path, repo: str) -> None:
        super().__init__(store)
        if not repo:
            raise RuntimeError("AQA_BUG_REPO 未配置，无法向 GitHub 提 issue")
        self.repo = repo

    def create(self, bug: dict[str, Any]) -> dict[str, Any]:
        labels = ",".join(bug.get("labels", ["bug", "auto-filed"]))
        cmd = [
            "gh",
            "issue",
            "create",
            "--repo",
            self.repo,
            "--title",
            bug["title"],
            "--body",
            bug["body_markdown"],
            "--label",
            labels,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"gh issue create 失败：{proc.stderr[:400]}")
        url = proc.stdout.strip().splitlines()[-1]
        payload = {
            **bug,
            "id": url.rsplit("/", 1)[-1],
            "url": url,
            "status": "open",
        }
        self.record(bug["fingerprint"], payload)
        return payload

    def confirm(self, bug_id: str, accepted: bool, note: str = "") -> dict[str, Any]:
        """人工在 issue 上确认后回写：accepted 则加 confirmed 标签，否则关闭。"""
        data = self._load()
        for fp, bug in data.items():
            if bug.get("id") == bug_id:
                action = (
                    ["gh", "issue", "edit", bug_id, "--repo", self.repo, "--add-label", "confirmed"]
                    if accepted
                    else ["gh", "issue", "close", bug_id, "--repo", self.repo, "--reason", "not planned"]
                )
                subprocess.run(action, capture_output=True, text=True, timeout=120)
                bug["status"] = "confirmed" if accepted else "rejected"
                bug["confirm_note"] = note
                self._save(data)
                return bug
        raise KeyError(f"未找到缺陷 {bug_id}")


def build_tracker(settings: Any) -> BugTracker:
    store = Path(settings.workspace) / ".aqa" / "bugs.json"
    if settings.bugtracker_kind == "github":
        return GithubBugTracker(store, settings.bug_repo)
    return LocalBugTracker(store)
