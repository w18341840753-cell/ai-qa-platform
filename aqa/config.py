"""配置：全部通过环境变量 / .env 注入，便于 CI 与本地共用同一套代码。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """极简 .env 解析：只支持 KEY=VALUE，不覆盖已存在的环境变量。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key, str(default)).lower()
    return raw in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """一次运行的全部可调参数。"""

    # 工作目录：所有产物写入 <workspace>/runs/<run_id>/
    workspace: Path = field(default_factory=lambda: Path.cwd())

    # 模型：mock = 零依赖离线演示；api = 调真实模型
    model_mode: str = "mock"
    text_base_url: str = "https://api.deepseek.com/v1"
    text_api_key: str = ""
    text_model: str = "deepseek-chat"
    vision_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    vision_api_key: str = ""
    vision_model: str = "qwen-vl-max"
    model_timeout: int = 120

    # 适配器类型
    device_kind: str = "mock"          # mock | adb | serial
    device_id: str = "mock-device-01"
    build_kind: str = "mock"           # mock | docker
    bugtracker_kind: str = "local"     # local | github
    bug_repo: str = ""                 # github 提 issue 的目标仓库 owner/name

    # 设备相关
    serial_port: str = ""
    flash_cmd: str = ""
    case_timeout: int = 120

    # 产品生成（AI 原生的第一步：代码也由 AI 写）
    generate_product: bool = False
    product_requirement: str = (
        "为智能中控屏设备开发固件：设备上电后进入主界面；支持 Wi-Fi 配网并连接云端；"
        "支持 OTA 远程升级并生效；断网后能自动重连。需提供可在本地运行的模拟实现，"
        "输出串口日志与最终状态，供自动化测试驱动。"
    )

    # 流程控制
    max_retries: int = 2
    require_human_confirm: bool = True   # 提 bug 后是否要人工确认
    auto_confirm_in_demo: bool = True    # mock 模式下自动确认，演示完整闭环

    @classmethod
    def from_env(cls, workspace: Path | None = None) -> "Settings":
        root = workspace or Path.cwd()
        load_dotenv(root / ".env")
        return cls(
            workspace=root,
            model_mode=_env("AQA_MODEL_MODE", "mock"),
            text_base_url=_env("AQA_TEXT_BASE_URL", "https://api.deepseek.com/v1"),
            text_api_key=_env("AQA_TEXT_API_KEY"),
            text_model=_env("AQA_TEXT_MODEL", "deepseek-chat"),
            vision_base_url=_env(
                "AQA_VISION_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
            vision_api_key=_env("AQA_VISION_API_KEY"),
            vision_model=_env("AQA_VISION_MODEL", "qwen-vl-max"),
            model_timeout=int(_env("AQA_MODEL_TIMEOUT", "120")),
            device_kind=_env("AQA_DEVICE_KIND", "mock"),
            device_id=_env("AQA_DEVICE_ID", "mock-device-01"),
            build_kind=_env("AQA_BUILD_KIND", "mock"),
            bugtracker_kind=_env("AQA_BUGTRACKER_KIND", "local"),
            bug_repo=_env("AQA_BUG_REPO"),
            serial_port=_env("AQA_SERIAL_PORT"),
            flash_cmd=_env("AQA_FLASH_CMD"),
            case_timeout=int(_env("AQA_CASE_TIMEOUT", "120")),
            generate_product=_env_bool("AQA_GENERATE_PRODUCT", False),
            product_requirement=_env("AQA_REQUIREMENT") or cls.product_requirement,
            max_retries=int(_env("AQA_MAX_RETRIES", "2")),
            require_human_confirm=_env_bool("AQA_REQUIRE_HUMAN_CONFIRM", True),
            auto_confirm_in_demo=_env_bool("AQA_AUTO_CONFIRM", True),
        )

    def validate(self) -> None:
        """跑真实模型时提前暴露配置缺失，避免跑到第六层才炸。"""
        if self.model_mode != "api":
            return
        missing = []
        if not self.text_api_key:
            missing.append("AQA_TEXT_API_KEY")
        if not self.vision_api_key:
            missing.append("AQA_VISION_API_KEY")
        if missing:
            raise SystemExit(
                f"model_mode=api 但缺少配置：{', '.join(missing)}\n"
                f"请复制 .env.example 为 .env 并填入密钥，或用 --mock 演示模式。"
            )
