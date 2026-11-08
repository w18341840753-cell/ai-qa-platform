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
    text_model: str = "deepseek-flash"
    # 视觉通道默认复用同一个 DeepSeek 端点（deepseek-flash 支持图像输入），
    # 有 Qwen-VL / 本地 VLM 时改 AQA_VISION_* 即可，不必换掉整套配置。
    vision_base_url: str = "https://api.deepseek.com/v1"
    vision_api_key: str = ""
    vision_model: str = "deepseek-flash"
    model_timeout: int = 120
    # 推理模型会先花 token 思考，预算给小了会直接返回空内容
    max_tokens_text: int = 8192
    max_tokens_vision: int = 2048
    # 写代码是长输出任务，DeepSeek 这类推理模型 8k 会被思考吃光、必须给到 32k
    max_tokens_gen: int = 32768
    # 判定优先级：state_first = 设备状态可解析时先按状态判定，视觉只在状态缺失时兜底
    judge_mode: str = "state_first"

    # 成本核算（元 / 百万 token），用于报告里的用量行
    price_in_per_m: float = 1.0
    price_out_per_m: float = 4.0

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
        text_base = _env("AQA_TEXT_BASE_URL", "https://api.deepseek.com/v1")
        text_key = _env("AQA_TEXT_API_KEY")
        vision_base = _env("AQA_VISION_BASE_URL", text_base)
        # 视觉通道没单独配 key 时复用文本 key（同一个 DeepSeek 账号即可跑通）
        vision_key = _env("AQA_VISION_API_KEY") or (
            text_key if vision_base == text_base else ""
        )
        return cls(
            workspace=root,
            model_mode=_env("AQA_MODEL_MODE", "mock"),
            text_base_url=text_base,
            text_api_key=text_key,
            text_model=_env("AQA_TEXT_MODEL", "deepseek-flash"),
            vision_base_url=vision_base,
            vision_api_key=vision_key,
            vision_model=_env("AQA_VISION_MODEL", "deepseek-flash"),
            model_timeout=int(_env("AQA_MODEL_TIMEOUT", "120")),
            max_tokens_text=int(_env("AQA_MAX_TOKENS", "8192")),
            max_tokens_vision=int(_env("AQA_MAX_TOKENS_VISION", "2048")),
            max_tokens_gen=int(_env("AQA_MAX_TOKENS_GEN", "32768")),
            judge_mode=_env("AQA_JUDGE_MODE", "state_first"),
            price_in_per_m=float(_env("AQA_PRICE_IN", "1.0")),
            price_out_per_m=float(_env("AQA_PRICE_OUT", "4.0")),
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
            missing.append("AQA_VISION_API_KEY（或让 AQA_VISION_BASE_URL 与文本端点一致以复用同一个 key）")
        if missing:
            raise SystemExit(
                f"model_mode=api 但缺少配置：{', '.join(missing)}\n"
                f"请复制 .env.example 为 .env 并填入密钥，或用 --mock 演示模式。"
            )
