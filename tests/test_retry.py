"""重试策略回归测试：模型阶段不再整阶段重发，改为调用内重试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aqa.config import Settings
from aqa.context import RunContext
from aqa.orchestrator import Orchestrator
from aqa.stages import PlanStage, Stage


def _settings(workspace: Path, **kw) -> Settings:
    return Settings(
        workspace=workspace,
        model_mode="mock",
        device_kind="mock",
        build_kind="mock",
        bugtracker_kind="local",
        **kw,
    )


class _AlwaysFailStage(Stage):
    name = "always-fail"
    label = "必失败"

    def __init__(self, retry_attempts: int | None = None) -> None:
        self.retry_attempts = retry_attempts
        self.calls = 0

    def run(self, ctx, rt):  # noqa: ANN001
        self.calls += 1
        raise RuntimeError("模拟阶段失败")


class OrchestratorRetryTest(unittest.TestCase):
    def _run(self, stage: Stage, max_retries: int = 2):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("aqa.orchestrator.time.sleep", return_value=None):
                _, outcomes = Orchestrator(
                    _settings(Path(tmp), max_retries=max_retries), stages=[stage]
                ).run(repo="demo/retry", quiet=True)
        return outcomes

    def test_stage_with_explicit_one_attempt_is_not_retried(self) -> None:
        stage = _AlwaysFailStage(retry_attempts=1)
        outcomes = self._run(stage)
        self.assertEqual(stage.calls, 1)
        self.assertEqual(outcomes[0].attempts, 1)
        self.assertFalse(outcomes[0].ok)

    def test_stage_without_override_still_uses_settings_retries(self) -> None:
        stage = _AlwaysFailStage()
        outcomes = self._run(stage, max_retries=2)
        self.assertEqual(stage.calls, 3)
        self.assertEqual(outcomes[0].attempts, 3)


class _OkStage(Stage):
    name = "ok-stage"
    label = "正常"

    def run(self, ctx, rt):  # noqa: ANN001
        return {"total": 1}


class ConsoleSafetyTest(unittest.TestCase):
    def test_print_encoding_failure_does_not_fail_run(self) -> None:
        """GBK 控制台下打印 ✅ 会抛 UnicodeEncodeError，不能因此把阶段判失败。"""
        boom = UnicodeEncodeError("gbk", "\u2705", 0, 1, "illegal multibyte sequence")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("builtins.print", side_effect=boom):
                _, outcomes = Orchestrator(
                    _settings(Path(tmp)), stages=[_OkStage()]
                ).run(repo="demo/console", quiet=False)
        self.assertTrue(outcomes[0].ok)
        self.assertEqual(outcomes[0].attempts, 1)


class _ScriptedText:
    """按剧本返回模型输出，用来精确统计调用次数。"""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0

    def complete(self, system: str, prompt: str, max_tokens: int | None = None) -> str:
        self.calls += 1
        index = min(self.calls - 1, len(self.outputs) - 1)
        return self.outputs[index]


_GOOD_PLAN = json.dumps(
    {
        "rationale": "覆盖变更",
        "cases": [
            {
                "id": "TC-01",
                "title": "启动",
                "priority": "P0",
                "scenario": "boot",
                "steps": ["上电"],
                "expect": "进入主界面",
                "expect_state": {"ok": True},
                "needs_injection": False,
            }
        ],
    },
    ensure_ascii=False,
)


class _Device:
    def capabilities(self) -> dict:
        return {"scenarios": ["boot", "provision", "ota", "reconnect"], "injection": True}


class PlanRetryTest(unittest.TestCase):
    def _rt(self, root: Path, text) -> SimpleNamespace:  # noqa: ANN001
        return SimpleNamespace(
            settings=Settings(workspace=root, text_model="model"),
            models=SimpleNamespace(text=text),
            device=_Device(),
        )

    def _ctx(self, root: Path) -> RunContext:
        ctx = RunContext(
            run_id="r1",
            repo="org/repo",
            commit="abc",
            artifacts=root / "runs" / "r1",
        )
        ctx.data["trigger"] = {
            "repo": "org/repo",
            "commit": "abc",
            "changed_files": ["a"],
        }
        return ctx

    def test_bad_json_retries_once_inside_the_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = _ScriptedText(["这不是 JSON", "也不是"])
            with self.assertRaises(RuntimeError):
                PlanStage().run(self._ctx(root), self._rt(root, text))
            self.assertEqual(text.calls, 2, "阶段只应调用模型两次（含一次重试）")

    def test_recovers_on_the_internal_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = _ScriptedText(["坏输出", _GOOD_PLAN])
            plan = PlanStage().run(self._ctx(root), self._rt(root, text))
            self.assertEqual(text.calls, 2)
            self.assertEqual(plan["cases"][0]["id"], "TC-01")
            self.assertFalse(plan.get("cached", False))

    def test_empty_plan_is_an_error_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = json.dumps({"rationale": "", "cases": []})
            text = _ScriptedText([empty])
            with self.assertRaises(RuntimeError):
                PlanStage().run(self._ctx(root), self._rt(root, text))
            self.assertEqual(text.calls, 2)


if __name__ == "__main__":
    unittest.main()
