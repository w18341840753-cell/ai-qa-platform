"""判定短路与模型调用参数的回归测试。

覆盖：
  - 硬故障信号命中时跳过视觉调用，泛化 ERROR 不误判
  - 视觉判定返回不可用时重试一次，仍失败按 FAIL
  - 模型请求带上 max_tokens / response_format
  - 默认文本模型为 deepseek-flash
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aqa.config import Settings
from aqa.llm import OpenAICompatClient
from aqa.stages import judge_by_vision, rule_fail_signal


class RuleScreenTest(unittest.TestCase):
    def test_hard_signal_short_circuits(self) -> None:
        trace = {"logs": "[uart] system_reset triggered by prov_watchdog"}
        self.assertEqual(rule_fail_signal(trace), "system_reset")

    def test_state_ok_false_short_circuits(self) -> None:
        trace = {"logs": 'STATE {"screen": "home", "ok": false}'}
        self.assertEqual(rule_fail_signal(trace), "STATE ok=false")

    def test_generic_error_is_left_to_model(self) -> None:
        # 负向用例可能故意打印 ERROR，不能作为硬失败信号
        trace = {"logs": '[uart] ERROR: invalid input\nSTATE {"ok": true}'}
        self.assertEqual(rule_fail_signal(trace), "")

    def test_clean_logs_pass_through(self) -> None:
        trace = {"logs": "[uart] screen=home\nSTATE {\"ok\": true}"}
        self.assertEqual(rule_fail_signal(trace), "")


class _FlakyVision:
    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, system, prompt, image_path):
        self.calls += 1
        if self.calls == 1:
            return "这不是 JSON"
        return '{"verdict": "PASS", "confidence": 0.9, "reason": "ok"}'


class _BrokenVision:
    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, system, prompt, image_path):
        self.calls += 1
        raise RuntimeError("endpoint down")


def _rt(vision) -> SimpleNamespace:
    return SimpleNamespace(models=SimpleNamespace(vision=vision))


class JudgeRetryTest(unittest.TestCase):
    def test_retries_once_on_bad_json(self) -> None:
        vision = _FlakyVision()
        result = judge_by_vision(_rt(vision), "sys", "prompt", None)
        self.assertEqual(vision.calls, 2)
        self.assertEqual(result["verdict"], "PASS")

    def test_falls_back_to_fail_after_two_attempts(self) -> None:
        vision = _BrokenVision()
        result = judge_by_vision(_rt(vision), "sys", "prompt", None)
        self.assertEqual(vision.calls, 2)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["confidence"], 0.0)


class PayloadTest(unittest.TestCase):
    @staticmethod
    def _capture(client: OpenAICompatClient) -> dict:
        captured: dict = {}

        def fake_post(payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": "{}"}}]}

        client._post = fake_post  # type: ignore[method-assign]
        client.chat([{"role": "user", "content": "hi"}])
        return captured

    def test_text_client_requests_json_and_caps_tokens(self) -> None:
        client = OpenAICompatClient("http://x/v1", "k", "deepseek-flash", max_tokens=8192, json_mode=True)
        payload = self._capture(client)
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_vision_client_keeps_plain_mode(self) -> None:
        client = OpenAICompatClient("http://x/v1", "k", "qwen-vl-max", max_tokens=1024)
        payload = self._capture(client)
        self.assertEqual(payload["max_tokens"], 1024)
        self.assertNotIn("response_format", payload)


class DefaultsTest(unittest.TestCase):
    def test_default_text_model_is_current(self) -> None:
        self.assertEqual(Settings().text_model, "deepseek-flash")
        with tempfile.TemporaryDirectory() as tmp:
            # 用空目录避免读到本地 .env 覆盖
            self.assertEqual(Settings.from_env(Path(tmp)).text_model, "deepseek-flash")


if __name__ == "__main__":
    unittest.main()
