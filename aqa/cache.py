"""按 commit 缓存产物目录与 JSON；缓存故障一律静默降级。

缓存只解决「同一 commit 重复跑」的浪费：命中即跳过模型调用，
但契约自检仍会执行，避免旧缓存掩盖 contract.py 的升级。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any


class ArtifactCache:
    """本地产物缓存，目录结构为 <cache_dir>/<namespace>/<key>[.json]。"""

    MAX_ENTRIES = 20

    def __init__(self, settings: Any) -> None:
        workspace = Path(settings.workspace)
        cache_dir = getattr(settings, "cache_dir", None)
        self.root = Path(cache_dir) if cache_dir else workspace / ".aqa" / "cache"
        self.enabled = bool(getattr(settings, "cache_enabled", True))

    def key(self, *parts: Any) -> str:
        """把输入片段稳定序列化后取 sha256 前 16 位。"""
        raw = json.dumps(
            parts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------ JSON

    def get_json(self, namespace: str, key: str) -> dict[str, Any] | None:
        """读取 JSON 缓存；未命中或读取失败返回 None。"""
        if not self.enabled:
            return None
        path = self._json_path(namespace, key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缓存损坏等同于未命中
            return None
        if not isinstance(payload, dict):
            return None
        self._touch(path)
        return payload

    def put_json(self, namespace: str, key: str, payload: Any) -> None:
        """写入 JSON 缓存；任何异常都不向调用方抛出。"""
        if not self.enabled:
            return
        path = self._json_path(namespace, key)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, path)
            self._prune(namespace)
        except Exception:  # noqa: BLE001 - 缓存写失败不影响本次运行
            self._remove(tmp)

    # ------------------------------------------------------------------ 目录

    def get_dir(self, namespace: str, key: str, dest: Path) -> bool:
        """命中时把缓存目录复制到 dest 并返回 True。"""
        if not self.enabled:
            return False
        src = self._dir_path(namespace, key)
        dest = Path(dest)
        try:
            if not src.is_dir():
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest, dirs_exist_ok=True)
        except Exception:  # noqa: BLE001 - 复制失败按未命中处理
            return False
        self._touch(src)
        return True

    def put_dir(self, namespace: str, key: str, src: Path) -> None:
        """把整个目录复制进缓存；任何异常都不向调用方抛出。"""
        if not self.enabled:
            return
        src = Path(src)
        dest = self._dir_path(namespace, key)
        tmp = self._namespace_dir(namespace) / f".{key}.{uuid.uuid4().hex}.tmp"
        try:
            if not src.is_dir():
                return
            tmp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, tmp)
            self._remove(dest)
            os.replace(tmp, dest)
            self._touch(dest)
            self._prune(namespace)
        except Exception:  # noqa: BLE001 - 缓存写失败不影响本次运行
            self._remove(tmp)

    # ------------------------------------------------------------------ 内部

    def _namespace_dir(self, namespace: str) -> Path:
        return self.root / namespace

    def _json_path(self, namespace: str, key: str) -> Path:
        return self._namespace_dir(namespace) / f"{key}.json"

    def _dir_path(self, namespace: str, key: str) -> Path:
        return self._namespace_dir(namespace) / key

    def _prune(self, namespace: str) -> None:
        """每个命名空间只保留最近访问/写入的 20 个 key。"""
        try:
            ns_dir = self._namespace_dir(namespace)
            entries: dict[str, float] = {}
            for child in ns_dir.iterdir():
                if child.name.startswith("."):
                    continue
                if child.is_dir():
                    entry_key = child.name
                elif child.is_file() and child.suffix == ".json":
                    entry_key = child.stem
                else:
                    continue
                mtime = child.stat().st_mtime
                entries[entry_key] = max(entries.get(entry_key, 0.0), mtime)

            excess = len(entries) - self.MAX_ENTRIES
            if excess <= 0:
                return
            oldest = sorted(entries.items(), key=lambda item: item[1])[:excess]
            for entry_key, _ in oldest:
                self._remove(ns_dir / f"{entry_key}.json")
                self._remove(ns_dir / entry_key)
        except Exception:  # noqa: BLE001 - 淘汰失败不影响本次运行
            return

    @staticmethod
    def _touch(path: Path) -> None:
        try:
            os.utime(path, None)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        except Exception:  # noqa: BLE001
            pass
