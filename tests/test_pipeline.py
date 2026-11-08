"""端到端测试：mock 模型 + mock 设备，验证九层闭环与去重逻辑。

不需要任何 API Key、不需要真实设备：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aqa.config import Settings
from aqa.orchestrator import Orchestrator


def _settings(workspace: Path) -> Settings:
    return Settings(
        workspace=workspace,
        model_mode="mock",
        device_kind="mock",
        build_kind="mock",
        bugtracker_kind="local",
        max_retries=0,
    )


class MockPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_full_pipeline_produces_report_and_bugs(self) -> None:
        ctx, outcomes = Orchestrator(_settings(self.ws)).run(
            repo="demo/panel", commit="abc1234", quiet=True
        )

        self.assertTrue(all(o.ok for o in outcomes), [o.__dict__ for o in outcomes])

        report = json.loads((ctx.artifacts / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["total"], 4)
        self.assertEqual(report["passed"], 2)
        self.assertEqual(report["failed"], 2)
        self.assertEqual(report["pass_rate"], 50.0)

        bugs = json.loads((ctx.artifacts / "bugs.json").read_text(encoding="utf-8"))
        self.assertEqual(len(bugs), 2, "两个不同根因应各建一条缺陷")
        self.assertTrue(all(b["status"] == "confirmed" for b in bugs))

        # 产物齐备
        for name in ("testplan.json", "traces.json", "verdicts.json", "timeline.jsonl"):
            self.assertTrue((ctx.artifacts / name).exists(), f"缺少产物 {name}")

    def test_same_root_cause_is_deduped_across_runs(self) -> None:
        first_ctx, _ = Orchestrator(_settings(self.ws)).run(repo="demo/panel", quiet=True)
        first_bugs = json.loads((first_ctx.artifacts / "bugs.json").read_text(encoding="utf-8"))
        self.assertEqual(len(first_bugs), 2, "两个不同证据键应各建一条缺陷")
        self.assertTrue(all(b["created_this_run"] for b in first_bugs))

        second_ctx, _ = Orchestrator(_settings(self.ws)).run(repo="demo/panel", quiet=True)
        second_bugs = json.loads((second_ctx.artifacts / "bugs.json").read_text(encoding="utf-8"))
        self.assertEqual(len(second_bugs), 2, "第二轮仍是同样两条缺陷")
        self.assertFalse(any(b["created_this_run"] for b in second_bugs),
                         "同一证据键第二次运行不应重复建缺陷")

    def test_judge_flags_injected_defects(self) -> None:
        ctx, _ = Orchestrator(_settings(self.ws)).run(repo="demo/panel", quiet=True)
        verdicts = json.loads((ctx.artifacts / "verdicts.json").read_text(encoding="utf-8"))
        failed_cases = {v["case_id"] for v in verdicts if v["verdict"] == "FAIL"}
        self.assertEqual(failed_cases, {"TC-02", "TC-03"})
        for v in verdicts:
            if v["verdict"] == "FAIL":
                self.assertIn("root_cause", v.get("rca", {}))
        # 日志里有硬故障信号的用例走规则短路，其余仍交给视觉模型
        self.assertEqual(
            {v["case_id"]: v["judged_by"] for v in verdicts},
            {"TC-01": "vision", "TC-02": "rule", "TC-03": "rule", "TC-04": "vision"},
        )


class DemoPipelineTest(unittest.TestCase):
    """AI 原生链路：AI 写代码 → 打包 → 部署到模拟设备 → 真实执行 → 判定。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.settings = Settings(
            workspace=self.ws,
            model_mode="mock",
            device_kind="sim",
            build_kind="package",
            bugtracker_kind="local",
            generate_product=True,
            max_retries=0,
            case_timeout=60,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_generated_product_actually_runs_on_device(self) -> None:
        ctx, outcomes = Orchestrator(self.settings).run(repo="demo/generated", quiet=True)
        self.assertTrue(all(o.ok for o in outcomes), [o.__dict__ for o in outcomes])

        product = json.loads((ctx.artifacts / "product.json").read_text(encoding="utf-8"))
        self.assertIn("main.py", product["files"])
        self.assertTrue((ctx.artifacts / "product" / "main.py").exists())

        artifact = json.loads((ctx.artifacts / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(artifact["kind"], "firmware-package")
        self.assertTrue(Path(artifact["path"]).exists())

        # 关键断言：执行阶段是**真的运行了产品进程**，日志来自设备自身输出
        traces = json.loads((ctx.artifacts / "traces.json").read_text(encoding="utf-8"))
        provision = next(t for t in traces if t["case_id"] == "TC-02")
        self.assertIn("[uart] prov_retry", provision["logs"])
        self.assertIn("scenario=provision", " ".join(provision["steps"]))

        verdicts = json.loads((ctx.artifacts / "verdicts.json").read_text(encoding="utf-8"))
        self.assertEqual({v["case_id"] for v in verdicts if v["verdict"] == "FAIL"},
                         {"TC-02", "TC-03"})

    def test_screen_snapshot_is_rendered(self) -> None:
        ctx, _ = Orchestrator(self.settings).run(repo="demo/generated", quiet=True)
        screen = (ctx.artifacts / "traces" / "TC-02.screen.txt").read_text(encoding="utf-8")
        self.assertIn("boot_loop", screen)
        self.assertIn("故障", screen)


if __name__ == "__main__":
    unittest.main()
