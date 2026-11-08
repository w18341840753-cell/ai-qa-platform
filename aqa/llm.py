"""模型客户端：纯标准库实现 OpenAI 兼容协议，mock 模式零依赖可跑。

分工（重要）：
  - TextModel   → 测试计划设计、失败归因、bug 描述撰写（deepseek-flash 等文本模型）
  - VisionModel → 看设备截图 / 相机画面，判断界面状态是否正确（Qwen-VL 等视觉模型）
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LLMError(RuntimeError):
    """模型调用失败（网络 / 鉴权 / 返回结构异常）。"""


# ---------------------------------------------------------------- 用量计量


@dataclass
class Usage:
    """累计 token 用量，用于在报告里给出真实成本，而不是靠感觉估。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    def add(self, payload: dict[str, Any] | None) -> None:
        self.calls += 1
        if not payload:
            return
        self.prompt_tokens += int(payload.get("prompt_tokens") or 0)
        self.completion_tokens += int(payload.get("completion_tokens") or 0)

    def merge(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls

    def cost(self, price_in: float, price_out: float) -> float:
        """price_in / price_out 单位：元每百万 token。"""
        return (
            self.prompt_tokens / 1_000_000 * price_in
            + self.completion_tokens / 1_000_000 * price_out
        )

    def to_dict(self, price_in: float = 0.0, price_out: float = 0.0) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "cost_cny": round(self.cost(price_in, price_out), 4),
        }


# ---------------------------------------------------------------- JSON 兜底解析


def extract_json(raw: str) -> dict[str, Any]:
    """模型偶尔会用 ```json 包裹或加解释文字，这里做容错解析。"""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1)
    else:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            raw = raw[start : end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型返回无法解析为 JSON：{exc}\n原始输出前 400 字：{raw[:400]}") from exc


# ---------------------------------------------------------------- 真实客户端


class OpenAICompatClient:
    """适配任何 OpenAI 兼容端点：DeepSeek、DashScope（通义千问）、vLLM 本地部署等。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 120,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.json_mode = json_mode
        self.usage = Usage()

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            raise LLMError(f"HTTP {exc.code} from {self.base_url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(f"无法连接模型端点 {self.base_url}：{exc}") from exc

    def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        budget = max_tokens or self.max_tokens
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
            "max_tokens": budget,
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(payload)
        self.usage.add(data.get("usage"))

        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError) as exc:
            raise LLMError(f"返回结构异常：{json.dumps(data)[:400]}") from exc

        content = (message.get("content") or "").strip()
        reasoning = message.get("reasoning_content") or ""
        finish = choice.get("finish_reason") or "unknown"

        # 被截断的输出必然是残 JSON / 残代码，返回它只会让下游报「解析失败」，
        # 在这里就说清楚，让调用方有机会重试或换策略。
        if finish == "length" and content:
            raise LLMError(
                f"模型 {self.model} 输出被截断（finish_reason=length，max_tokens={budget}，"
                f"已输出 {len(content)} 字）：请调大 AQA_MAX_TOKENS 或把任务拆小"
            )
        if content:
            return content

        # 推理模型（deepseek-flash / v4-pro）会先把 token 花在思考上，
        # 预算给小了就会出现 content 为空 —— 这里要把原因说清楚，而不是返回空串。
        detail = (
            f"模型 {self.model} 返回空内容（finish_reason={finish}，"
            f"max_tokens={budget}，思考内容 {len(reasoning)} 字）"
        )
        if finish == "length" or reasoning:
            detail += "：推理模型的思考过程把 max_tokens 耗尽了，请调大 AQA_MAX_TOKENS"
        raise LLMError(detail)


class ApiTextModel:
    def __init__(self, client: OpenAICompatClient) -> None:
        self.client = client

    def complete(self, system: str, prompt: str, max_tokens: int | None = None) -> str:
        return self.client.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )


class ApiVisionModel:
    def __init__(self, client: OpenAICompatClient) -> None:
        self.client = client

    def analyze(self, system: str, prompt: str, image_path: Path | None) -> str:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if image_path and image_path.exists():
            b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            )
        return self.client.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": content}]
        )


# ---------------------------------------------------------------- Mock 实现


FAIL_HINTS = (
    "ERROR",
    "boot_loop",
    "ota_failed",
    "version_mismatch",
    "未上线",
    "版本号不匹配",
    "无响应",
    "花屏",
    "崩溃",
)


class MockTextModel:
    """离线演示用：按提示词关键词返回结构化结果，让整条链路能跑通。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, system: str, prompt: str, max_tokens: int | None = None) -> str:
        self.calls.append(prompt[:60])
        time.sleep(0.05)  # 模拟网络往返

        if "测试计划" in prompt or "test_plan" in prompt:
            return json.dumps(
                {
                    "rationale": "检测到本次提交修改了网络配网与 OTA 版本号逻辑，"
                    "回归风险集中在首次启动、配网、升级三条主路径。",
                    "cases": [
                        {
                            "id": "TC-01",
                            "title": "冷启动后进入待机界面",
                            "priority": "P0",
                            "scenario": "boot",
                            "steps": ["上电", "等待 30s", "读取屏幕状态"],
                            "expect": "屏幕显示待机主界面，无异常重启",
                        },
                        {
                            "id": "TC-02",
                            "title": "Wi-Fi 首次配网并在云端上线",
                            "priority": "P0",
                            "scenario": "provision",
                            "steps": ["进入配网模式", "下发 SSID/密码", "等待云端在线"],
                            "expect": "30s 内 MQTT 连接到云端并上报设备信息",
                        },
                        {
                            "id": "TC-03",
                            "title": "OTA 升级后版本号校验",
                            "priority": "P0",
                            "scenario": "ota",
                            "steps": ["推送固件更新", "等待升级完成", "读取版本号"],
                            "expect": "版本号与目标固件一致，升级后自动重启",
                        },
                        {
                            "id": "TC-04",
                            "title": "断网后自动重连",
                            "priority": "P1",
                            "scenario": "reconnect",
                            "steps": ["断开网络 60s", "恢复网络", "检查在线状态"],
                            "expect": "恢复后 60s 内重新上线",
                        },
                    ],
                },
                ensure_ascii=False,
            )

        if "归因" in prompt or "root_cause" in prompt:
            # 按失败现象分流，模拟真实模型对不同场景给出不同根因
            if "版本号" in prompt or "OTA" in prompt or "ota" in prompt:
                return json.dumps(
                    {
                        "severity": "P0",
                        "root_cause": "OTA 升级写入新分区后未更新 boot 分区指针，回滚到旧固件 v1.4.0",
                        "evidence": "串口日志 ota_done 后 version 仍为 v1.4.0，version_check_mismatch",
                        "suggestion": "升级完成后显式校验 otadata 分区并设置 boot slot，再触发重启",
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "severity": "P0",
                    "root_cause": "配网状态机在收到云端 ACK 前被超时分支重置，导致重试循环",
                    "evidence": "串口日志出现 wifi_prov_retry=5 后紧接 system_reset",
                    "suggestion": "在 prov_state 中增加 ACK 等待态，超时阈值由 10s 放宽到 30s",
                },
                ensure_ascii=False,
            )

        if "bug" in prompt or "缺陷" in prompt:
            if "版本号" in prompt or "OTA" in prompt or "ota" in prompt:
                return json.dumps(
                    {
                        "title": "【OTA 回归】升级成功但版本号仍为旧固件，疑似 boot 分区未切换",
                        "body_markdown": "## 现象\nOTA 流程返回成功，重启后版本号仍为 v1.4.0。\n\n"
                        "## 复现步骤\n1. 推送 v1.4.2 固件\n2. 等待升级完成并重启\n3. 读取版本号\n\n"
                        "## 期望\n版本号变为 v1.4.2\n\n## 实际\nv1.4.0，且不会再次触发升级",
                        "labels": ["bug", "auto-filed", "OTA"],
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "title": "【配网回归】配网成功但设备反复重启，云端始终离线",
                    "body_markdown": "## 现象\n配网下发后设备未上线，串口反复输出 reset。\n\n"
                    "## 复现步骤\n1. 上电\n2. 下发 Wi-Fi 凭据\n3. 观察 60s\n\n"
                    "## 期望\n30s 内云端在线\n\n## 实际\n设备在 10s 超时后重启，循环 5 次",
                    "labels": ["bug", "auto-filed", "配网"],
                },
                ensure_ascii=False,
            )

        return '{"note": "mock 模型：未命中场景，返回占位结果"}'


class MockVisionModel:
    """离线演示用：从提示词里的 SCREEN_HINT 判断 PASS/FAIL。"""

    def analyze(self, system: str, prompt: str, image_path: Path | None) -> str:
        time.sleep(0.05)
        hit = next((h for h in FAIL_HINTS if h in prompt), None)
        if hit:
            return json.dumps(
                {
                    "verdict": "FAIL",
                    "confidence": 0.86,
                    "reason": f"截图/日志显示异常：{hit}",
                    "visual_evidence": f"SCREEN_HINT={hit}",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "verdict": "PASS",
                "confidence": 0.92,
                "reason": "界面状态与预期一致，无异常元素",
                "visual_evidence": "SCREEN_HINT=normal",
            },
            ensure_ascii=False,
        )


# ---------------------------------------------------------------- 工厂


@dataclass
class Models:
    text: Any
    vision: Any
    mode: str
    text_client: Any = None
    vision_client: Any = None

    def usage(self, price_in: float = 0.0, price_out: float = 0.0) -> dict[str, Any]:
        """汇总两侧模型用量，用于报告里的成本核算。"""
        total = Usage()
        for client in (self.text_client, self.vision_client):
            if client is not None:
                total.merge(client.usage)
        return total.to_dict(price_in, price_out)


def build_models(settings: Any) -> Models:
    if settings.model_mode == "mock":
        return Models(text=MockTextModel(), vision=MockVisionModel(), mode="mock")

    text_client = OpenAICompatClient(
        settings.text_base_url,
        settings.text_api_key,
        settings.text_model,
        settings.model_timeout,
        max_tokens=settings.max_tokens_text,
        json_mode=True,  # 文本侧三处调用（计划/归因/bug）都要求 JSON
    )
    vision_client = OpenAICompatClient(
        settings.vision_base_url,
        settings.vision_api_key,
        settings.vision_model,
        settings.model_timeout,
        max_tokens=settings.max_tokens_vision,
    )
    return Models(
        text=ApiTextModel(text_client),
        vision=ApiVisionModel(vision_client),
        mode="api",
        text_client=text_client,
        vision_client=vision_client,
    )
