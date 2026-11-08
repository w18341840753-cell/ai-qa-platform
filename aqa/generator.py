"""产品代码生成器：让 AI 从一句需求，生成一个**可运行**的被测产品项目。

这是「AI 原生」的起点 —— 测试平台测试的对象本身，也由 AI 生成。

mock 模式：输出内置的智能中控屏固件模拟器（Python 实现，行为等价于嵌入式固件，
         含两处真实缺陷，供后续测试链路真实地发现它们）
api  模式：由 DeepSeek 按需求生成文件树，写入磁盘后可被直接构建、部署、执行
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------- Mock 生成器


class MockProductGenerator:
    """不调模型，直接落一份可运行的固件模拟器（含两处真实缺陷）。"""

    kind = "mock"

    def generate(self, requirement: str, out_dir: Path) -> GeneratedProduct:
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


GENERATE_SYSTEM = (
    "你是嵌入式固件工程师。根据需求生成一个可在本地直接运行的 Python 项目，"
    "用于模拟设备行为以便自动化测试驱动。要求：\n"
    "1. 必须有一个入口脚本 main.py，支持命令行参数指定场景，例如 boot / provision / ota / reconnect\n"
    "2. 运行后向标准输出打印串口风格日志（以 [uart] 开头），最后一行打印 'STATE {json}'\n"
    "   其中 json 至少包含 screen / online / version / ok 四个键\n"
    "3. 不要依赖任何第三方库\n"
    "只输出 JSON，格式：{\"files\": [{\"path\": \"main.py\", \"content\": \"...\"}]}"
)


class ApiProductGenerator:
    kind = "api"

    def __init__(self, text_model: Any) -> None:
        self.text_model = text_model

    def generate(self, requirement: str, out_dir: Path) -> GeneratedProduct:
        prompt = (
            f"需求如下：\n{requirement}\n\n"
            f"请生成完整项目文件树（包含 {PRODUCT_NAME}/ 目录内的所有文件内容）。"
        )
        raw = self.text_model.complete(GENERATE_SYSTEM, prompt)
        data = extract_json(raw)
        entries = data.get("files") or []
        if not entries:
            raise RuntimeError("模型未返回任何文件")

        files: dict[str, str] = {}
        for item in entries:
            path = str(item.get("path", "")).lstrip("./")
            content = item.get("content", "")
            if not path or not isinstance(content, str):
                continue
            # 防目录穿越：只允许项目内的相对路径
            if ".." in Path(path).parts or re.match(r"^[a-zA-Z]:", path):
                continue
            files[path] = content
        if "main.py" not in files:
            raise RuntimeError("生成结果缺少 main.py，无法作为可执行产品")

        out_dir.mkdir(parents=True, exist_ok=True)
        written = _write_tree(out_dir, files)
        return GeneratedProduct(
            name=PRODUCT_NAME,
            path=out_dir,
            requirement=requirement,
            files=written,
            generator="api",
        )


def build_generator(models: Any) -> Any:
    """按模型模式选择生成器；api 模式失败时可降级到 mock（由调用方决定）。"""
    if models.mode == "api":
        return ApiProductGenerator(models.text)
    return MockProductGenerator()


def load_state(stdout: str) -> dict[str, Any]:
    """从设备输出里解析最后一行 STATE。"""
    for line in reversed(stdout.splitlines()):
        if line.startswith("STATE "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                return {}
    return {}
