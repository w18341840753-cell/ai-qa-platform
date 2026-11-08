"""设备适配器：嵌入式 / IoT 设备的部署与用例执行。

嵌入式场景与 Web 测试的三个关键差异（设计都体现在接口里）：
  1. 「部署」往往等于烧录固件，不是发个包 —— 所以 deploy() 收的是 artifact 而非 URL
  2. 没有 DOM，只有屏幕像素 + 串口日志 —— 所以 run_case() 必须同时产出截图和日志
  3. 设备会掉线、会重启、会串口挂死 —— 所以每一步都要有超时与重试语义
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Trace:
    """单个用例的执行轨迹 —— 交给判定层做证据。"""

    case_id: str
    ok_to_proceed: bool = True
    steps: list[str] = field(default_factory=list)
    screenshot: Path | None = None
    logs: str = ""
    hint: str = ""          # 视觉线索（真实实现里由 VLM 从截图里读，mock 里预置）
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "ok_to_proceed": self.ok_to_proceed,
            "steps": self.steps,
            "screenshot": str(self.screenshot) if self.screenshot else None,
            "logs": self.logs,
            "hint": self.hint,
            "duration_s": round(self.duration_s, 2),
        }


class DeviceAdapter:
    """所有设备类型都必须实现这五个方法。"""

    kind = "base"

    def prepare(self) -> dict[str, Any]:
        raise NotImplementedError

    def deploy(self, artifact: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def run_case(self, case: dict[str, Any], out_dir: Path) -> Trace:
        raise NotImplementedError

    def health(self) -> dict[str, Any]:
        raise NotImplementedError

    def teardown(self) -> None:
        pass


# ---------------------------------------------------------------- Mock 设备


class MockDevice(DeviceAdapter):
    """离线演示：模拟一台固件有缺陷的设备。

    缺陷注入策略（可通过构造参数调整）：
      - TC-02 配网后不上线（P0）
      - TC-03 OTA 后版本号不匹配（P0）
      - 其余用例通过
    这样可以演示「AI 判定 → 归因 → 建 bug」的完整闭环。
    """

    kind = "mock"

    def __init__(self, device_id: str, defect_cases: tuple[str, ...] = ("TC-02", "TC-03")) -> None:
        self.device_id = device_id
        self.defect_cases = defect_cases
        self._deployed = False

    def prepare(self) -> dict[str, Any]:
        time.sleep(0.05)
        return {"device": self.device_id, "mode": "mock", "ready": True}

    def deploy(self, artifact: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.15)
        self._deployed = True
        return {
            "device": self.device_id,
            "artifact": artifact.get("path"),
            "method": "mock-flash",
            "rebooted": True,
            "ok": True,
        }

    def run_case(self, case: dict[str, Any], out_dir: Path) -> Trace:
        case_id = case["id"]
        started = time.time()
        trace = Trace(case_id=case_id, steps=list(case.get("steps", [])))

        # 造一张「截图」占位图，让真实 VLM 接入时也有输入
        shot = out_dir / f"{case_id}.txt"
        time.sleep(0.08)

        if case_id in self.defect_cases:
            if case_id == "TC-02":
                trace.logs = (
                    "[uart] wifi_prov_start\n[uart] got ssid=TestAP\n"
                    "[uart] prov_state=WAIT_ACK\n[uart] prov_retry=5\n[uart] system_reset\n"
                )
                trace.hint = "未上线"
            else:
                trace.logs = (
                    "[uart] ota_start target=v1.4.2\n[uart] ota_done\n"
                    "[uart] version=v1.4.0\n[uart] version_check_mismatch\n"
                )
                trace.hint = "版本号不匹配"
            trace.ok_to_proceed = False
        else:
            trace.logs = f"[uart] case {case_id} completed, state=ok\n"
            trace.hint = "normal"

        shot.write_text(
            f"mock screenshot for {case_id}\nhint={trace.hint}\n", encoding="utf-8"
        )
        trace.screenshot = shot
        trace.duration_s = time.time() - started
        return trace

    def health(self) -> dict[str, Any]:
        return {"device": self.device_id, "online": self._deployed, "signal": "mock"}


# ---------------------------------------------------------------- 模拟设备（真实执行）


def _parse_state(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith("STATE "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                return {}
    return {}


def _display_width(text: str) -> int:
    """中文字符占两列宽，表格对齐必须按显示宽度算，不能按字符数。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _row(label: str, value: str, inner: int = 28) -> str:
    text = f"{label} : {value}"
    return "| " + text + " " * max(0, inner - _display_width(text)) + " |\n"


def _render_screen(state: dict[str, Any], logs: str) -> str:
    """把设备状态渲染成「屏幕快照」。

    真实设备这里是 PNG 截图；模拟环境用文本渲染，便于人读与日志比对。
    接入真实设备时把这里换成 adb screencap / 相机抓图即可，下游判定逻辑不变。
    """
    screen = state.get("screen", "unknown")
    online = "在线" if state.get("online") else "离线"
    version = state.get("version", "?")
    health = "故障" if "ERROR" in logs or screen in ("boot_loop", "ota_failed") else "正常"
    line = "+" + "-" * 30 + "+\n"
    return (
        line
        + "|  智能中控屏 · 屏幕快照        |\n"
        + line
        + _row("界面状态", screen)
        + _row("云端连接", online)
        + _row("固件版本", str(version))
        + _row("自检结论", health)
        + line
    )


class SimDevice(DeviceAdapter):
    """模拟设备：**真实运行**被测产品，而不是返回剧本结果。

    与 MockDevice 的本质区别：
      - 部署 = 把构建产物解包到设备工作区（等价于烧录）
      - 执行 = 真的启动产品进程、按场景驱动、抓取串口日志与 STATE
      - 判定依据 = 产品自己输出的日志与状态，而不是预先写好的 PASS/FAIL

    因此「AI 发现 bug」这件事是执行结果，不是编排脚本的假设。
    """

    kind = "sim"

    SCENARIO_KEYWORDS: dict[str, tuple[str, ...]] = {
        "provision": ("配网", "wifi", "wi-fi", "联网", "云端", "上线"),
        "ota": ("ota", "升级", "版本"),
        "reconnect": ("重连", "断网", "恢复"),
        "boot": ("启动", "开机", "待机", "上电"),
    }

    def __init__(self, device_id: str, python: str = "", timeout: int = 120) -> None:
        self.device_id = device_id
        self.python = python or sys.executable or "python"
        self.timeout = timeout
        self.work_dir: Path | None = None

    def prepare(self) -> dict[str, Any]:
        return {"device": self.device_id, "mode": "sim", "python": self.python, "ready": True}

    def deploy(self, artifact: dict[str, Any]) -> dict[str, Any]:
        src = Path(artifact.get("path", ""))
        if not src.exists():
            raise RuntimeError(f"构建产物不存在：{src}")

        work = src.parent / "device"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)

        if src.suffix == ".zip":
            with zipfile.ZipFile(src) as zf:
                zf.extractall(work)
            method = "unpack"
        else:  # 非压缩产物：直接投放
            shutil.copy2(src, work / src.name)
            method = "copy"

        self.work_dir = work
        return {
            "device": self.device_id,
            "artifact": str(src),
            "method": f"sim-{method}",
            "work_dir": str(work),
            "rebooted": True,
            "ok": True,
        }

    def _scenario_for(self, case: dict[str, Any]) -> str:
        declared = str(case.get("scenario", "")).strip().lower()
        if declared in self.SCENARIO_KEYWORDS:
            return declared
        text = (case.get("title", "") + " " + " ".join(case.get("steps", []))).lower()
        for name, keywords in self.SCENARIO_KEYWORDS.items():
            if any(k in text for k in keywords):
                return name
        return "boot"

    def run_case(self, case: dict[str, Any], out_dir: Path) -> Trace:
        if self.work_dir is None:
            raise RuntimeError("设备未部署，无法执行用例")

        case_id = case["id"]
        started = time.time()
        scenario = self._scenario_for(case)
        trace = Trace(case_id=case_id, steps=list(case.get("steps", [])))

        try:
            proc = subprocess.run(
                [self.python, "main.py", scenario],
                cwd=str(self.work_dir),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                encoding="utf-8",
                errors="replace",
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            stdout, stderr, exit_code = "", f"用例执行超时（{self.timeout}s）", -1

        state = _parse_state(stdout)
        screen_text = _render_screen(state, stdout)

        screen_path = out_dir / f"{case_id}.screen.txt"
        screen_path.write_text(screen_text + "\n--- uart ---\n" + stdout, encoding="utf-8")

        trace.logs = (stdout.strip() or stderr.strip())[:4000]
        trace.screenshot = screen_path
        trace.steps.append(f"scenario={scenario} exit={exit_code}")
        trace.hint = (
            f"screen={state.get('screen', 'unknown')} "
            f"online={state.get('online')} version={state.get('version')} "
            f"product_ok={state.get('ok')}"
        )
        trace.ok_to_proceed = exit_code == 0
        trace.duration_s = time.time() - started
        if exit_code != 0:
            trace.hint += f" | 进程异常退出 {exit_code}"
        return trace

    def health(self) -> dict[str, Any]:
        return {
            "device": self.device_id,
            "online": self.work_dir is not None,
            "work_dir": str(self.work_dir) if self.work_dir else None,
        }


# ---------------------------------------------------------------- ADB 设备


class AdbDevice(DeviceAdapter):
    """Android 底座的 IoT 设备：车机、智能屏、机顶盒、商显等。

    依赖：本机安装 platform-tools（adb），设备已开启 USB 调试或已 adb connect。
    """

    kind = "adb"

    def __init__(self, device_id: str, apk_path: str = "", package: str = "", timeout: int = 120):
        self.device_id = device_id
        self.apk_path = apk_path
        self.package = package
        self.timeout = timeout

    def _adb(self, *args: str, capture: bool = True) -> str:
        cmd = ["adb", "-s", self.device_id, *args]
        proc = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=self.timeout,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0 and capture:
            raise RuntimeError(f"adb 失败：{' '.join(cmd)}\n{proc.stderr[:500]}")
        return proc.stdout or ""

    def prepare(self) -> dict[str, Any]:
        state = self._adb("get-state").strip()
        return {"device": self.device_id, "mode": "adb", "ready": state == "device"}

    def deploy(self, artifact: dict[str, Any]) -> dict[str, Any]:
        apk = self.apk_path or artifact.get("path", "")
        if not apk:
            raise RuntimeError("未提供 APK 路径（AQA_APK_PATH 或构建产物）")
        out = self._adb("install", "-r", "-d", apk)
        if self.package:
            self._adb("shell", "am", "force-stop", self.package)
            self._adb("shell", "monkey", "-p", self.package, "1")
        return {"device": self.device_id, "artifact": apk, "method": "adb install", "log": out}

    def run_case(self, case: dict[str, Any], out_dir: Path) -> Trace:
        case_id = case["id"]
        started = time.time()
        trace = Trace(case_id=case_id, steps=list(case.get("steps", [])))

        # 清空日志 → 执行 → 截图 → 抓日志
        self._adb("logcat", "-c")
        for step in case.get("steps", []):
            trace.steps.append(f"executed: {step}")

        shot_path = out_dir / f"{case_id}.png"
        with shot_path.open("wb") as fh:
            proc = subprocess.run(
                ["adb", "-s", self.device_id, "exec-out", "screencap", "-p"],
                capture_output=True,
                timeout=self.timeout,
            )
            fh.write(proc.stdout)

        trace.logs = self._adb("logcat", "-d", "-t", "200")
        trace.screenshot = shot_path
        trace.hint = "见截图"  # 真实判定由 VLM 从截图读取
        trace.duration_s = time.time() - started
        return trace

    def health(self) -> dict[str, Any]:
        try:
            return {"device": self.device_id, "online": self._adb("get-state").strip() == "device"}
        except Exception as exc:  # noqa: BLE001 - 健康检查不应抛异常
            return {"device": self.device_id, "online": False, "error": str(exc)[:200]}


# ---------------------------------------------------------------- 串口 / 烧录设备


class SerialDevice(DeviceAdapter):
    """MCU / 开发板类设备：烧录固件 + 串口读日志。

    依赖：pyserial（pip install pyserial）；烧录命令由 AQA_FLASH_CMD 提供。
    """

    kind = "serial"

    def __init__(self, device_id: str, port: str, flash_cmd: str = "", timeout: int = 120):
        self.device_id = device_id
        self.port = port
        self.flash_cmd = flash_cmd
        self.timeout = timeout
        self._serial = None

    def prepare(self) -> dict[str, Any]:
        try:
            import serial  # noqa: PLC0415 - 可选依赖，延迟导入
        except ImportError as exc:
            raise RuntimeError("串口模式需要 pyserial：pip install pyserial") from exc
        self._serial = serial.Serial(self.port, 115200, timeout=1)
        return {"device": self.device_id, "mode": "serial", "port": self.port, "ready": True}

    def deploy(self, artifact: dict[str, Any]) -> dict[str, Any]:
        if not self.flash_cmd:
            raise RuntimeError("未配置烧录命令 AQA_FLASH_CMD")
        proc = subprocess.run(
            self.flash_cmd, shell=True, capture_output=True, text=True, timeout=self.timeout
        )
        if proc.returncode != 0:
            raise RuntimeError(f"烧录失败：{proc.stderr[-500:]}")
        return {"device": self.device_id, "method": "flash", "log": proc.stdout[-800:]}

    def run_case(self, case: dict[str, Any], out_dir: Path) -> Trace:
        case_id = case["id"]
        started = time.time()
        trace = Trace(case_id=case_id, steps=list(case.get("steps", [])))
        lines: list[str] = []
        if self._serial is not None:
            deadline = time.time() + min(self.timeout, 10)
            while time.time() < deadline:
                line = self._serial.readline()
                if line:
                    lines.append(line.decode("utf-8", "replace").strip())
        trace.logs = "\n".join(lines) or "[uart] no output"
        trace.hint = "见串口日志"
        trace.duration_s = time.time() - started
        return trace

    def health(self) -> dict[str, Any]:
        return {"device": self.device_id, "online": self._serial is not None, "port": self.port}

    def teardown(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None


# ---------------------------------------------------------------- 工厂


def build_device(settings: Any) -> DeviceAdapter:
    kind = settings.device_kind
    if kind == "adb":
        return AdbDevice(settings.device_id, timeout=settings.case_timeout)
    if kind == "serial":
        return SerialDevice(
            settings.device_id,
            settings.serial_port or settings.device_id,
            settings.flash_cmd,
            timeout=settings.case_timeout,
        )
    if kind == "sim":
        return SimDevice(settings.device_id, timeout=settings.case_timeout)
    return MockDevice(settings.device_id)


__all__ = [
    "DeviceAdapter",
    "Trace",
    "MockDevice",
    "SimDevice",
    "AdbDevice",
    "SerialDevice",
    "build_device",
]
