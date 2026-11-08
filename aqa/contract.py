"""设备状态契约（device state contract）。

为什么需要这一层：

被测产品本身也是 AI 写的，它输出的状态字段名不可控 —— 可能叫 ``fw_version``
而不是 ``version``，可能把在线状态藏在 ``cloud.connected`` 里，可能用 ``result``
代替 ``ok``。判定层如果按写死的键名去读，就会把「字段名不一样」误判成
「设备故障」，进而产出一堆假 bug —— 这比漏报更糟，因为它污染缺陷库。

所以这里定义唯一权威契约，分三步走：

1. **归一化**：把任意合理命名映射成 4 个规范字段（screen / online / version / ok）
2. **自检**：产品生成后立刻实跑一次，检查它是否真的输出了可用状态
3. **修复**：自检不通过时，把违规点喂回模型让它改（由 generator 层执行）

规范字段：
    screen  : str   当前界面标识，如 home / provisioned / boot_loop
    online  : bool  是否已连接云端
    version : str   固件版本号
    ok      : bool  本场景是否执行成功
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

CANONICAL: tuple[str, ...] = ("screen", "online", "version", "ok")

# 别名按优先级排列：越靠前越可信。带点号的别名匹配完整路径（cloud.connected）。
ALIASES: dict[str, tuple[str, ...]] = {
    "screen": (
        "screen",
        "ui.screen",
        "ui_state",
        "screen_name",
        "view",
        "scene",
        "page",
        "display",
        "ui",
    ),
    "online": (
        "online",
        "is_online",
        "cloud.connected",
        "cloud_connected",
        "cloud.online",
        "cloud",
        "connected",
        "network.connected",
        "network",
        "net",
        "mqtt.connected",
        "mqtt",
    ),
    "version": (
        "version",
        "fw_version",
        "firmware_version",
        "firmware",
        "fw",
        "ver",
        "build_version",
    ),
    "ok": (
        "ok",
        "result",
        "success",
        "passed",
        "pass",
        "healthy",
        "self_check",
        "status",
    ),
}

# 这些界面标识意味着设备已经不在正常工作状态
BAD_SCREENS: tuple[str, ...] = (
    "boot_loop",
    "ota_failed",
    "crash",
    "panic",
    "error",
    "unknown",
    "black",
    "off",
)

_TRUE = {
    "true", "1", "yes", "y", "on", "online", "connected", "ok",
    "success", "pass", "passed", "up", "healthy", "normal", "ready", "done",
}
_FALSE = {
    "false", "0", "no", "n", "off", "offline", "disconnected", "fail",
    "failed", "error", "down", "unhealthy", "abnormal", "timeout",
}

# 允许 "STATE {...}" / "STATE: {...}" / "[uart] STATE {...}" 三种写法
_STATE_RE = re.compile(r"(?:^|\s)STATE\s*[:=]?\s*(\{.*\})\s*$", re.M)


# ---------------------------------------------------------------- 解析


def _flatten(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """把嵌套 dict 摊平成 ['cloud.connected', True] 这样的路径列表。"""
    items: list[tuple[str, Any]] = []
    if not isinstance(obj, dict):
        return items
    for raw_key, value in obj.items():
        key = f"{prefix}.{raw_key}" if prefix else str(raw_key)
        items.append((key, value))
        if isinstance(value, dict):
            items.extend(_flatten(value, key))
    return items


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, dict):
        for key in ("connected", "online", "ok", "status", "state", "value"):
            if key in value:
                return _coerce_bool(value[key])
        return None
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _coerce_version(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value).strip().strip('"').strip("'")
    return text or None


def _coerce(field: str, value: Any) -> Any:
    if field == "version":
        return _coerce_version(value)
    if field in ("online", "ok"):
        return _coerce_bool(value)
    if isinstance(value, (dict, bool)) or value is None:
        return None
    text = str(value).strip().strip('"').strip("'")
    return text or None


def _match(flat: list[tuple[str, Any]], alias: str) -> tuple[str, Any] | None:
    """按别名找字段：先精确匹配完整路径，再匹配末级键，取层级最浅的那个。"""
    exact = [(k, v) for k, v in flat if k.lower() == alias]
    if exact:
        return min(exact, key=lambda kv: kv[0].count("."))
    if "." in alias:
        return None
    tail = [(k, v) for k, v in flat if k.split(".")[-1].lower() == alias]
    if tail:
        return min(tail, key=lambda kv: kv[0].count("."))
    return None


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """把产品自报状态映射成规范字段；缺字段记录在 _contract.missing 里。"""
    flat = _flatten(raw) if isinstance(raw, dict) else []
    out: dict[str, Any] = {}
    mapped: dict[str, str] = {}

    for field in CANONICAL:
        for alias in ALIASES[field]:
            hit = _match(flat, alias)
            if hit is None:
                continue
            value = _coerce(field, hit[1])
            # 找到了键但值不可转换（比如 version 拿到了 bool），继续试下一个别名
            if value is None:
                continue
            out[field] = value
            mapped[field] = hit[0]
            break

    missing = [f for f in CANONICAL if f not in out]
    out["_contract"] = {
        "missing": missing,
        "mapped": mapped,
        "raw_keys": [k for k, _ in flat][:40],
    }
    return out


def extract_state(stdout: str) -> dict[str, Any]:
    """从标准输出里取最后一条 STATE，并归一化。找不到返回空状态。"""
    matches = _STATE_RE.findall(stdout or "")
    for chunk in reversed(matches):
        try:
            raw = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            return normalize(raw)
    return normalize({})


def sanitize_expectation(raw: Any) -> dict[str, Any]:
    """清洗模型给出的 expect_state：只留能客观比对、且不会误伤的字段。

    只收 online / ok 两个布尔值：
      - screen 的等值比对不可靠 —— 模型会臆造界面枚举（比如期望 provision，
        产品实际叫 provisioning），这属于用例设计问题，不该判成固件缺陷。
        界面是否异常由 BAD_SCREENS 兜底。
      - version 同理（模型猜的版本号常与产品常量不一致），版本是否生效
        交给 ota 场景的 ok 与日志判断。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("online", "ok"):
        if key not in raw:
            continue
        value = _coerce(key, raw[key])
        if value is not None:
            out[key] = value
    return out


def is_usable(state: dict[str, Any]) -> bool:
    """状态是否足以支撑判定：四个规范字段齐备且界面标识可辨识。"""
    if not state:
        return False
    if state.get("_contract", {}).get("missing"):
        return False
    screen = str(state.get("screen", "")).strip().lower()
    return bool(screen) and screen != "unknown"


def violations(state: dict[str, Any]) -> list[str]:
    """返回契约违规点，用于喂给模型做自修复。"""
    issues: list[str] = []
    missing = state.get("_contract", {}).get("missing", [])
    if missing:
        issues.append(f"STATE 缺少字段：{', '.join(missing)}")
    screen = str(state.get("screen", "")).strip().lower()
    if screen in ("", "unknown"):
        issues.append("screen 值为空或 unknown，无法反映当前界面")
    for field in ("online", "ok"):
        value = state.get(field)
        if field in state and not isinstance(value, bool):
            issues.append(f"{field} 必须是布尔值，当前为 {type(value).__name__}")
    return issues


# ---------------------------------------------------------------- 自检


def probe(
    project_dir: Path,
    entry: str = "main.py",
    scenario: str = "boot",
    python: str = "",
    timeout: int = 60,
) -> dict[str, Any]:
    """实跑一次产品，检查它是否遵守状态契约。

    这是「AI 自检」的证据来源：不是看代码写得像不像，而是看它跑起来有没有输出。
    带场景参数跑不通时（产品没实现 argparse），退化为无参再跑一次。
    """
    python = python or sys.executable or "python"
    project_dir = Path(project_dir)
    report: dict[str, Any] = {
        "ok": False,
        "entry": entry,
        "scenario": scenario,
        "command": [],
        "exit_code": None,
        "state": {},
        "missing": list(CANONICAL),
        "stdout_tail": "",
        "stderr_tail": "",
        "error": "",
    }

    if not (project_dir / entry).exists():
        report["error"] = f"未找到入口文件 {entry}"
        return report

    for command in ([python, entry, scenario], [python, entry]):
        try:
            proc = subprocess.run(
                command,
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            report["error"] = f"执行失败：{exc}"
            continue

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        state = extract_state(stdout)
        report.update(
            {
                "command": command,
                "exit_code": proc.returncode,
                "state": state,
                "missing": state.get("_contract", {}).get("missing", list(CANONICAL)),
                "stdout_tail": stdout[-1500:],
                "stderr_tail": stderr[-800:],
            }
        )

        if proc.returncode == 0 and state.get("_contract", {}).get("missing") != list(
            CANONICAL
        ):
            break  # 已经跑起来且输出了状态，不必再试无参形式

        # 只有「参数不被接受」这一种情况值得重试无参形式
        not_accepted = re.search(
            r"unrecognized arguments|invalid choice|no such option|usage:|"
            r"the following arguments are required|error:\s*argument",
            stderr,
            re.I,
        )
        if proc.returncode != 0 and not_accepted:
            continue
        break

    report["ok"] = (
        report.get("exit_code") == 0
        and not report["missing"]
        and str(report["state"].get("screen", "")).strip().lower() not in ("", "unknown")
    )
    report["violations"] = violations(report["state"])
    return report


def describe(report: dict[str, Any]) -> str:
    """把自检结果压成一行，用于日志与产物。"""
    if report.get("ok"):
        state = report.get("state", {})
        return (
            f"契约通过 screen={state.get('screen')} online={state.get('online')} "
            f"version={state.get('version')} ok={state.get('ok')}"
        )
    issues = report.get("violations") or [report.get("error") or "未输出 STATE"]
    return "契约不通过：" + "；".join(str(i) for i in issues)


__all__ = [
    "CANONICAL",
    "BAD_SCREENS",
    "normalize",
    "extract_state",
    "sanitize_expectation",
    "is_usable",
    "violations",
    "probe",
    "describe",
]
