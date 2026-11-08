"""构建适配器：把源码变成可部署产物（固件 / 镜像 / APK）。"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any


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

    def build(self, repo_dir: Path, out_dir: Path) -> dict[str, Any]:
        repo_dir = Path(repo_dir)
        if not repo_dir.exists():
            raise RuntimeError(f"产品目录不存在：{repo_dir}")

        out_dir.mkdir(parents=True, exist_ok=True)
        artifact = out_dir / "firmware.zip"
        files: list[str] = []
        with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(repo_dir.rglob("*")):
                if path.is_dir() or "__pycache__" in path.parts:
                    continue
                rel = path.relative_to(repo_dir)
                zf.write(path, rel.as_posix())
                files.append(rel.as_posix())

        payload = artifact.read_bytes()
        return {
            "kind": "firmware-package",
            "path": str(artifact),
            "sha256": hashlib.sha256(payload).hexdigest()[:16],
            "size_kb": round(len(payload) / 1024, 1),
            "files": files,
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
        return PackageBuild()
    return MockBuild()
