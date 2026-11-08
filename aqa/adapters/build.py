"""构建适配器：把源码变成可部署产物（固件 / 镜像 / APK）。"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

# 打包时必须跳过的目录：这些是「平台的产物」而不是「被测试的源码」。
# 少了这层过滤，webhook 模式下用工作目录当源码会把 runs/ 一起压进固件 ——
# 实测能写出 1.7 GB 的 firmware.zip 并卡死整条流水线。
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "runs",
    ".aqa",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".tox",
}


class BuildAdapter:
    kind = "base"

    def build(self, repo_dir: Path, out_dir: Path) -> dict[str, Any]:
        raise NotImplementedError


class MockBuild(BuildAdapter):
    """离线演示：生成一个假的固件产物，带 sha256。"""

    kind = "mock"

    def build(self, repo_dir: Path, out_dir: Path) -> dict[str, Any]:
        time.sleep(0.2)
        artifact = out_dir / "firmware-mock.bin"
        payload = f"mock-firmware {time.time()} {repo_dir}".encode()
        artifact.write_bytes(payload)
        return {
            "kind": "firmware",
            "path": str(artifact),
            "sha256": hashlib.sha256(payload).hexdigest()[:16],
            "size_kb": round(len(payload) / 1024, 1),
            "builder": "mock",
        }


class PackageBuild(BuildAdapter):
    """打包构建：把产品目录压成 zip，模拟「固件打包」这一步。

    真实项目里这里通常是 cmake/make 产出 .bin，或 gradle 产出 .apk；
    打包格式变了，但下游 deploy() 拿到的始终是「一个可部署产物」。
    """

    kind = "package"

    def __init__(self, max_mb: int | None = None) -> None:
        self.max_mb = int(max_mb or os.environ.get("AQA_MAX_ARTIFACT_MB", "50"))

    def _excluded(self, rel: Path) -> bool:
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            return True
        # 隐藏文件/目录（.env、.venv、.github 之类）不进产物
        return any(part.startswith(".") for part in rel.parts)

    def build(self, repo_dir: Path, out_dir: Path) -> dict[str, Any]:
        repo_dir = Path(repo_dir)
        if not repo_dir.exists():
            raise RuntimeError(f"产品目录不存在：{repo_dir}")

        out_dir.mkdir(parents=True, exist_ok=True)
        # 只有「输出目录位于源码目录内部」时才需要排除自己；
        # 反过来（源码在 runs/<id>/product、输出在 runs/<id>）不能排除，
        # 否则会把整个产品目录当成输出目录全部跳过，压出一个空包。
        out_abs = out_dir.resolve()
        repo_abs = repo_dir.resolve()
        out_inside_repo = out_abs != repo_abs and repo_abs in out_abs.parents
        artifact = out_dir / "firmware.zip"
        limit = self.max_mb * 1024 * 1024
        files: list[str] = []
        skipped = 0
        total = 0

        try:
            with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in sorted(repo_dir.rglob("*")):
                    if path.is_dir():
                        continue
                    rel = path.relative_to(repo_dir)
                    if self._excluded(rel):
                        skipped += 1
                        continue
                    resolved = path.resolve()
                    if out_inside_repo and (resolved == out_abs or out_abs in resolved.parents):
                        skipped += 1
                        continue
                    total += path.stat().st_size
                    if total > limit:
                        raise RuntimeError(
                            f"构建产物超过 {self.max_mb}MB 上限（已扫到 {total / 1024 / 1024:.1f}MB）。"
                            f"源码目录可能指错了：{repo_dir}。"
                            f"webhook 模式请用 AQA_SOURCE_DIR 指向真正的代码 checkout 目录，"
                            f"或调大 AQA_MAX_ARTIFACT_MB。"
                        )
                    zf.write(path, rel.as_posix())
                    files.append(rel.as_posix())
        except Exception:
            artifact.unlink(missing_ok=True)  # 不留半成品，避免下游误用
            raise

        payload = artifact.read_bytes()
        return {
            "kind": "firmware-package",
            "path": str(artifact),
            "sha256": hashlib.sha256(payload).hexdigest()[:16],
            "size_kb": round(len(payload) / 1024, 1),
            "files": files,
            "skipped": skipped,
            "source": str(repo_dir),
            "builder": "package",
        }


class DockerBuild(BuildAdapter):
    """真实构建：调用 docker build，产出镜像 tag。

    Dockerfile 与构建参数通过环境变量传入，避免把工程细节写死在平台里。
    """

    kind = "docker"

    def __init__(self, dockerfile: str = "Dockerfile", context: str = ".", tag: str = "") -> None:
        self.dockerfile = dockerfile
        self.context = context
        self.tag = tag

    def build(self, repo_dir: Path, out_dir: Path) -> dict[str, Any]:
        tag = self.tag or f"aqa-build:{int(time.time())}"
        cmd = [
            "docker",
            "build",
            "-f",
            str(repo_dir / self.dockerfile),
            "-t",
            tag,
            str(repo_dir / self.context),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        log_path = out_dir / "build.log"
        log_path.write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError(f"docker build 失败，日志见 {log_path}")
        return {
            "kind": "docker-image",
            "path": tag,
            "sha256": "",
            "builder": "docker",
            "log": str(log_path),
        }


def build_builder(settings: Any) -> BuildAdapter:
    if settings.build_kind == "docker":
        return DockerBuild()
    if settings.build_kind == "package":
        return PackageBuild(max_mb=getattr(settings, "max_artifact_mb", None))
    return MockBuild()
