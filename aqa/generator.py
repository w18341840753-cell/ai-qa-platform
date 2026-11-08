"""产品代码生成器：让 AI 从一句需求，生成一个**可运行**的被测产品项目。

这是「AI 原生」的起点 —— 测试平台测试的对象本身，也由 AI 生成。

mock 模式：输出内置的智能中控屏固件模拟器（Python 实现，行为等价于嵌入式固件，
         含两处真实缺陷，供后续测试链路真实地发现它们）
api  模式：由 DeepSeek 按需求生成文件树，写入磁盘后可被直接构建、部署、执行
"""

from __future__ import annotations

import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import contract
from .llm import extract_json

DEFAULT_REQUIREMENT = (
    "为智能中控屏设备开发固件：设备上电后进入主界面；支持 Wi-Fi 配网并连接云端；"
    "支持 OTA 远程升级并生效；断网后能自动重连。需提供可在本地运行的模拟实现，"
    "输出串口日志与最终状态，供自动化测试驱动。"
)

PRODUCT_NAME = "smart-panel"


# ---------------------------------------------------------------- 内置模板（mock）

_DEVICE_PY = '''"""智能中控屏固件模拟器。

用 Python 模拟嵌入式固件的运行时行为，供自动化测试平台驱动：
真实项目中这一层是 C 固件 + 串口/ADB 通讯，对外接口保持一致（串口日志 + 屏幕状态）。
"""

from __future__ import annotations

VERSION = "1.4.0"


class Panel:
    """设备状态机：boot / provision / ota / reconnect。"""

    def __init__(self) -> None:
        self.screen = "off"
        self.version = VERSION
        self.online = False
        self.logs: list[str] = []

    def _log(self, line: str) -> None:
        self.logs.append(f"[uart] {line}")

    # ------------------------------------------------------------ 上电启动

    def boot(self) -> bool:
        self._log("bootloader ok")
        self._log("kernel ok")
        self._log("ui_service started")
        self.screen = "home"
        self._log("screen=home")
        return True

    # ------------------------------------------------------------ Wi-Fi 配网

    def provision(self, ssid: str = "TestAP", ack_timeout_s: int = 10) -> bool:
        self._log(f"provision start ssid={ssid}")
        elapsed = 0
        for attempt in range(1, 6):
            elapsed += 2
            self._log(f"prov_retry={attempt} elapsed={elapsed}s")
            if elapsed >= ack_timeout_s:
                self._log("ERROR: ack_timeout, cloud ack not received")
                self._log("system_reset triggered by prov_watchdog")
                self.screen = "boot_loop"
                self.online = False
                return False
        self.online = True
        self.screen = "provisioned"
        self._log("mqtt connected")
        return True

    # ------------------------------------------------------------ OTA 升级

    def ota(self, target: str = "1.4.2") -> bool:
        self._log(f"ota_start target=v{target}")
        self._log("ota_write_ok")
        self._log("ota_done")
        # 升级完成后直接重启，未切换启动分区
        if self.version != target:
            self._log(f"version=v{self.version}")
            self._log("ERROR: version_mismatch after ota")
            self.screen = "ota_failed"
            return False
        return True

    # ------------------------------------------------------------ 断网重连

    def reconnect(self, offline_s: int = 60) -> bool:
        self._log(f"link_down {offline_s}s")
        self._log("link_up")
        self._log("mqtt reconnected")
        self.online = True
        self.screen = "home"
        return True
'''

_MAIN_PY = '''"""设备入口：python main.py <scenario>"""

from __future__ import annotations

import argparse
import json

from panel.device import Panel

SCENARIOS = {
    "boot": lambda p: p.boot(),
    "provision": lambda p: p.provision(),
    "ota": lambda p: p.ota(),
    "reconnect": lambda p: p.reconnect(),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="smart panel simulator")
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    args = parser.parse_args()

    panel = Panel()
    panel.boot()  # 任何场景都从上电开始
    ok = SCENARIOS[args.scenario](panel)

    for line in panel.logs:
        print(line)
    print("STATE " + json.dumps({
        "screen": panel.screen,
        "online": panel.online,
        "version": panel.version,
        "ok": ok,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_INIT_PY = '"""智能中控屏固件模拟包。"""\n\n__version__ = "1.4.0"\n'

_PRODUCT_README = """# {name}

由 AI 根据以下需求生成：

> {requirement}

## 运行

```bash
python main.py boot
python main.py provision
python main.py ota
python main.py reconnect
```

每个场景输出串口日志，最后一行是 `STATE {{...}}` 的结构化设备状态。
"""


# ---------------------------------------------------------------- 数据结构


@dataclass
class GeneratedProduct:
    name: str
    path: Path
    requirement: str
    files: list[str] = field(default_factory=list)
    generator: str = "mock"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "requirement": self.requirement,
            "files": self.files,
            "generator": self.generator,
        }


def _write_tree(root: Path, files: dict[str, str]) -> list[str]:
    written: list[str] = []
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


def _strip_common_prefix(files: dict[str, str]) -> dict[str, str]:
    """模型常把所有文件塞进一个同名目录（smart-panel/main.py）。

    对平台来说这是多了一层壳：入口不再是 main.py，构建产物也多一层目录。
    若所有路径共享同一个顶层目录，就把它剥掉。
    """
    parts = {path: Path(path).parts for path in files}
    if not parts:
        return files
    tops = {p[0] for p in parts.values() if len(p) > 1}
    if len(tops) == 1:
        top = tops.pop()
        if all(len(p) > 1 and p[0] == top for p in parts.values()):
            return {"/".join(p[1:]): files[path] for path, p in parts.items()}
    return files


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n```\s*$", re.S)


def _strip_fence(text: str) -> str:
    """模型爱把代码包在 ```python 里，落盘前要剥掉，否则文件跑不起来。"""
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text


def _sanitize_entries(entries: list[dict[str, Any]]) -> dict[str, str]:
    """过滤非法路径（目录穿越 / 绝对路径）并收集成 {相对路径: 内容}。"""
    files: dict[str, str] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).replace("\\", "/").lstrip("./")
        content = item.get("content", "")
        if not path or not isinstance(content, str):
            continue
        if ".." in Path(path).parts or re.match(r"^[a-zA-Z]:", path):
            continue
        files[path] = content
    return _strip_common_prefix(files)


def list_files(root: Path) -> list[str]:
    """列出项目内的源文件（相对路径），用于回填产物清单。"""
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )


# ---------------------------------------------------------------- Mock 生成器


class MockProductGenerator:
    """不调模型，直接落一份可运行的固件模拟器（含两处真实缺陷）。"""

    kind = "mock"

    def generate(self, requirement: str, out_dir: Path) -> GeneratedProduct:
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        files = {
            "README.md": _PRODUCT_README.format(name=PRODUCT_NAME, requirement=requirement),
            "main.py": _MAIN_PY,
            "panel/__init__.py": _INIT_PY,
            "panel/device.py": _DEVICE_PY,
        }
        written = _write_tree(out_dir, files)
        return GeneratedProduct(
            name=PRODUCT_NAME,
            path=out_dir,
            requirement=requirement,
            files=written,
            generator="mock",
        )


# ---------------------------------------------------------------- API 生成器


REPAIR_SYSTEM = (
    "你是嵌入式固件工程师，正在修复一个**自检不通过**的项目。"
    "自检会真实运行入口脚本并解析最后一行 STATE，因此必须让程序跑起来并输出合规状态。\n"
    "契约要求：python main.py <scenario> 后 stdout 打印 [uart] 日志，最后一行是\n"
    "STATE {\"screen\": \"...\", \"online\": true, \"version\": \"...\", \"ok\": true}\n"
    "四个键必须是 screen / online / version / ok，online 与 ok 为 JSON 布尔值。\n"
    "只输出需要修改的文件（整文件覆盖，不要输出未改动的文件），格式："
    "{\"files\": [{\"path\": \"main.py\", \"content\": \"...\"}], \"note\": \"改了什么\"}"
)

# 一次性让模型吐出整个项目文件树，很容易撞上 max_tokens 被截断（残 JSON 直接不可用）。
# 所以拆成两步：先出清单，再逐文件生成 —— 单次输出小、失败可局部重试。
MANIFEST_SYSTEM = (
    "你是嵌入式固件工程师。这一步只规划文件树，不写代码。"
    "根据需求列出实现所需的文件路径（项目内相对路径，最多 8 个），必须包含根目录 main.py。"
    "只输出 JSON：{\"files\": [\"main.py\", \"panel/device.py\"]}"
)

FILE_SYSTEM = (
    "你是嵌入式固件工程师。为下面的项目生成**单个文件**的完整内容，不要省略、不要写 TODO。"
    "整个项目的契约：入口 main.py 接受 boot / provision / ota / reconnect 四个场景参数；"
    "运行后先打印以 [uart] 开头的串口日志，最后一行输出 "
    "STATE {\"screen\": \"home\", \"online\": false, \"version\": \"1.0.0\", \"ok\": true}；"
    "键只能是 screen / online / version / ok，online 与 ok 是 JSON 布尔值；只用标准库。"
    "只输出 JSON：{\"content\": \"文件全文\"}"
)


class ApiProductGenerator:
    kind = "api"

    # 写代码是长输出任务：DeepSeek 这类推理模型 8k 预算会被思考吃光（content 为空），
    # 实测必须给到 32k 才稳定 stop。
    def __init__(self, text_model: Any, max_tokens: int = 32768) -> None:
        self.text_model = text_model
        self.max_tokens = max_tokens

    # ------------------------------------------------------------ 第一步：文件清单

    def _plan_files(self, requirement: str) -> list[str]:
        """只要清单，不要代码。失败就退化成单文件产品 —— 能跑比结构完整更重要。"""
        try:
            raw = self.text_model.complete(
                MANIFEST_SYSTEM,
                f"需求：\n{requirement}\n\n请列出实现该需求所需的文件。",
                max_tokens=2048,
            )
            entries = extract_json(raw).get("files") or []
        except Exception:  # noqa: BLE001 - 清单失败不应阻断生成
            entries = []

        paths: list[str] = []
        for item in entries:
            if isinstance(item, dict):
                item = item.get("path", "")
            path = str(item).replace("\\", "/").lstrip("./").strip()
            if not path or ".." in Path(path).parts or re.match(r"^[a-zA-Z]:", path):
                continue
            if path not in paths:
                paths.append(path)

        if "main.py" not in paths:
            paths.insert(0, "main.py")
        return paths[:8]

    # ------------------------------------------------------------ 第二步：逐文件生成

    def _file_content(
        self, requirement: str, paths: list[str], path: str, main_src: str
    ) -> str:
        """生成单个文件；被截断时自动翻倍预算重试一次。"""
        context = (
            f"需求：\n{requirement}\n\n"
            f"项目文件清单：{', '.join(paths)}\n"
            f"本次要生成的文件：{path}\n"
        )
        if main_src and path != "main.py":
            context += f"\n已生成的 main.py（请严格按它的导入路径写）：\n```python\n{main_src}\n```\n"

        budget = self.max_tokens
        last_error = ""
        for _ in range(2):
            try:
                raw = self.text_model.complete(FILE_SYSTEM, context, max_tokens=budget)
            except Exception as exc:  # noqa: BLE001 - 截断/空内容都值得加预算重试
                last_error = str(exc)
                budget *= 2
                continue
            try:
                content = extract_json(raw).get("content", "")
            except Exception:  # noqa: BLE001 - 模型直接吐代码时按原文收下
                content = raw
            if isinstance(content, str) and content.strip():
                return _strip_fence(content)
            last_error = "模型返回空内容"
            budget *= 2
        raise RuntimeError(f"{path} 生成失败：{last_error}")

    def generate(self, requirement: str, out_dir: Path) -> GeneratedProduct:
        if out_dir.exists():
            shutil.rmtree(out_dir)  # 清掉上一轮残留，避免新旧文件混在一起
        out_dir.mkdir(parents=True, exist_ok=True)

        paths = self._plan_files(requirement)
        written: list[str] = []
        errors: list[str] = []

        # main.py 必须先单独生成：其余文件要按它的导入路径写。
        if "main.py" in paths:
            try:
                main_src = self._file_content(requirement, paths, "main.py", "")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"main.py: {exc}")
                main_src = ""
            else:
                (out_dir / "main.py").write_text(main_src, encoding="utf-8")
                written.append("main.py")
                main_src = main_src[:2500]

        others = [p for p in paths if p != "main.py"]
        if others and written:
            # 其余文件互不依赖，并行生成：串行要 5 分钟，3 并发降到 2 分钟以内
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {
                    pool.submit(self._file_content, requirement, paths, path, main_src): path
                    for path in others
                }
                for future, path in futures.items():
                    try:
                        content = future.result()
                    except Exception as exc:  # noqa: BLE001 - 单文件失败不影响其它文件
                        errors.append(f"{path}: {exc}")
                        continue
                    target = out_dir / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                    written.append(path)

        if "main.py" not in written:
            raise RuntimeError(f"未能生成根目录 main.py：{'；'.join(errors) or '模型无输出'}")

        return GeneratedProduct(
            name=PRODUCT_NAME,
            path=out_dir,
            requirement=requirement,
            files=sorted(written),
            generator="api",
        )

    def repair(self, project_dir: Path, report: dict[str, Any], requirement: str) -> list[str]:
        """把自检违规点喂回模型，让它只改需要改的文件。

        只做一轮——修不好就带着「契约不通过」的标记继续跑，让判定层按
        「无法判定」处理，而不是反复烧 token。
        """
        entry = project_dir / "main.py"
        current = entry.read_text(encoding="utf-8", errors="replace")[:4000] if entry.exists() else ""
        prompt = (
            f"需求：{requirement}\n\n"
            f"自检结果：ok={report.get('ok')} exit_code={report.get('exit_code')}\n"
            f"违规点：{'；'.join(report.get('violations') or []) or '无'}\n"
            f"解析到的状态：{json.dumps(report.get('state') or {}, ensure_ascii=False)}\n"
            f"stdout 尾部：\n{(report.get('stdout_tail') or '')[-1500:]}\n"
            f"stderr 尾部：\n{(report.get('stderr_tail') or '')[-600:]}\n\n"
            f"当前 main.py：\n```python\n{current}\n```\n\n"
            "请输出需要修改的文件。"
        )
        raw = self.text_model.complete(REPAIR_SYSTEM, prompt, max_tokens=self.max_tokens)
        files = _sanitize_entries(extract_json(raw).get("files") or [])
        if not files:
            return []
        return _write_tree(project_dir, files)


def build_generator(models: Any, settings: Any = None) -> Any:
    """按模型模式选择生成器；api 模式失败时可降级到 mock（由调用方决定）。"""
    if models.mode == "api":
        budget = getattr(settings, "max_tokens_gen", 32768) if settings else 32768
        return ApiProductGenerator(models.text, max_tokens=budget)
    return MockProductGenerator()


def load_state(stdout: str) -> dict[str, Any]:
    """从设备输出里解析最后一行 STATE（已归一化到状态契约）。"""
    return contract.extract_state(stdout)
