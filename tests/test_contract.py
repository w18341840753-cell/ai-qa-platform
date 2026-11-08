"""设备状态契约的回归测试。

这里的每一条都对应一次真模型跑出来的事故：
  - AI 生成的固件把版本号写成 fw_version、在线状态写成 cloud.connected
    → 判定层读不到 → 4 条假 bug
  - 状态完全读不出来时仍然给 FAIL → 缺陷库被垃圾填满

所以：别名要能归一化；读不到要判「无法判定」，而不是失败。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aqa import contract
from aqa.config import Settings
from aqa.context import RunContext
from aqa.stages import JudgeStage, Runtime, judge_by_state


class NormalizeTest(unittest.TestCase):
    def test_alias_naming_is_mapped(self) -> None:
        raw = {
            "ui_state": "home",
            "cloud": {"connected": True},
            "fw_version": "1.4.2",
            "result": "success",
        }
        state = contract.normalize(raw)
        self.assertEqual(state["screen"], "home")
        self.assertIs(state["online"], True)
        self.assertEqual(state["version"], "1.4.2")
        self.assertIs(state["ok"], True)
        self.assertEqual(state["_contract"]["missing"], [])

    def test_canonical_keys_win_over_aliases(self) -> None:
        state = contract.normalize({"version": "2.0.0", "fw_version": "1.0.0"})
        self.assertEqual(state["version"], "2.0.0")

    def test_string_booleans_are_coerced(self) -> None:
        state = contract.normalize({"screen": "home", "online": "true", "ok": "false"})
        self.assertIs(state["online"], True)
        self.assertIs(state["ok"], False)

    def test_missing_fields_are_reported(self) -> None:
        state = contract.normalize({"screen": "home"})
        self.assertEqual(set(state["_contract"]["missing"]), {"online", "version", "ok"})
        self.assertFalse(contract.is_usable(state))


class ExtractStateTest(unittest.TestCase):
    def test_uart_prefix_and_colon_are_tolerated(self) -> None:
        stdout = '[uart] boot ok\n[uart] STATE: {"screen": "home", "online": true, "version": "1", "ok": true}'
        state = contract.extract_state(stdout)
        self.assertEqual(state["screen"], "home")
        self.assertTrue(contract.is_usable(state))

    def test_last_state_wins(self) -> None:
        stdout = (
            'STATE {"screen": "boot", "online": false, "version": "1", "ok": true}\n'
            'STATE {"screen": "home", "online": true, "version": "1", "ok": true}\n'
        )
        self.assertEqual(contract.extract_state(stdout)["screen"], "home")

    def test_no_state_line(self) -> None:
        self.assertFalse(contract.is_usable(contract.extract_state("[uart] nothing here")))


class ProbeTest(unittest.TestCase):
    def test_probe_accepts_aliased_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entry = Path(tmp) / "main.py"
            entry.write_text(
                "import json\n"
                "print('[uart] boot ok')\n"
                "print('STATE ' + json.dumps({'ui_state': 'home', 'cloud': {'connected': True},"
                " 'fw_version': '1.0.0', 'result': True}))\n",
                encoding="utf-8",
            )
            report = contract.probe(Path(tmp))
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["state"]["version"], "1.0.0")

    def test_probe_flags_missing_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "main.py").write_text("print('[uart] boot ok')\n", encoding="utf-8")
            report = contract.probe(Path(tmp))
            self.assertFalse(report["ok"])
            self.assertTrue(report["violations"])
            self.assertEqual(report["missing"], list(contract.CANONICAL))

    def test_probe_reports_missing_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = contract.probe(Path(tmp))
            self.assertFalse(report["ok"])
            self.assertIn("main.py", report["error"])


class JudgeByStateTest(unittest.TestCase):
    def _state(self, **kwargs):
        base = {"screen": "home", "online": True, "version": "1.0.0", "ok": True}
        base.update(kwargs)
        return contract.normalize(base)

    def test_provision_online_passes(self) -> None:
        result = judge_by_state({"scenario": "provision"}, self._state(), {})
        self.assertEqual(result["verdict"], "PASS")

    def test_provision_offline_fails(self) -> None:
        result = judge_by_state({"scenario": "provision"}, self._state(online=False), {})
        self.assertEqual(result["verdict"], "FAIL")

    def test_boot_into_boot_loop_fails(self) -> None:
        result = judge_by_state({"scenario": "boot"}, self._state(screen="boot_loop"), {})
        self.assertEqual(result["verdict"], "FAIL")

    def test_unusable_state_returns_none(self) -> None:
        self.assertIsNone(judge_by_state({"scenario": "boot"}, contract.normalize({}), {}))

    def test_unknown_scenario_defers_to_vision(self) -> None:
        self.assertIsNone(judge_by_state({"scenario": "stress"}, self._state(), {}))

    def test_expect_state_overrides_scenario_default(self) -> None:
        # 反向用例：错误凭据期望离线。设备却上线了 → 必须判失败，
        # 不能被 provision 的场景默认值（要求在线）判成 PASS。
        case = {"scenario": "provision", "expect_state": {"online": False}}
        result = judge_by_state(case, self._state(online=True), {})
        self.assertEqual(result["verdict"], "FAIL")

    def test_expect_state_match_passes(self) -> None:
        case = {"scenario": "provision", "expect_state": {"online": True, "screen": "provisioned"}}
        result = judge_by_state(case, self._state(screen="provisioned"), {})
        self.assertEqual(result["verdict"], "PASS")


class ExpectationTest(unittest.TestCase):
    def test_only_boolean_keys_survive(self) -> None:
        # screen / version 的等值比对会误伤（模型臆造界面名、猜错版本号），必须丢掉
        cleaned = contract.sanitize_expectation(
            {"online": "true", "screen": "home", "version": "9.9.9", "noise": 1}
        )
        self.assertEqual(cleaned, {"online": True})

    def test_non_dict_returns_empty(self) -> None:
        self.assertEqual(contract.sanitize_expectation("online=true"), {})


class _FailVision:
    def analyze(self, system, prompt, image_path):
        return json.dumps({"verdict": "FAIL", "confidence": 0.7, "reason": "看不清"})


class InconclusiveTest(unittest.TestCase):
    """状态读不出来 + 非硬故障信号 → 不能报缺陷。"""

    def test_missing_contract_downgrades_fail_to_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = RunContext(run_id="t", repo="r", commit="c", artifacts=Path(tmp))
            ctx.data["plan"] = {
                "cases": [{"id": "TC-01", "title": "冷启动", "scenario": "boot", "expect": "进入主界面"}]
            }
            ctx.data["execute"] = {
                "traces": [
                    {
                        "case_id": "TC-01",
                        "logs": "[uart] 设备无响应",
                        "hint": "screenshot missing",
                        "screenshot": None,
                        "contract_ok": False,
                        "state": {},
                    }
                ]
            }
            rt = Runtime(
                settings=Settings(),
                models=SimpleNamespace(mode="api", text=SimpleNamespace(complete=lambda s, p: "{}"), vision=_FailVision()),
                device=SimpleNamespace(),
                builder=SimpleNamespace(),
                tracker=SimpleNamespace(),
                repo_dir=Path(tmp),
            )
            result = JudgeStage().run(ctx, rt)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["inconclusive"], 1)
            self.assertEqual(result["verdicts"][0]["verdict"], "INCONCLUSIVE")
            self.assertNotIn("rca", result["verdicts"][0])


class CapabilityTest(unittest.TestCase):
    """设备能力决定用例能不能跑 —— 跑不了要标未执行，而不是判失败。"""

    def test_sim_rejects_injection_cases(self) -> None:
        from aqa.adapters.device import SimDevice

        ok, why = SimDevice("sim-1").supports(
            {"scenario": "provision", "needs_injection": True}
        )
        self.assertFalse(ok)
        self.assertIn("注入", why)

    def test_sim_accepts_supported_scenario(self) -> None:
        from aqa.adapters.device import SimDevice

        ok, _ = SimDevice("sim-1").supports({"scenario": "ota"})
        self.assertTrue(ok)

    def test_mock_allows_everything(self) -> None:
        from aqa.adapters.device import MockDevice

        ok, _ = MockDevice("mock-1").supports(
            {"scenario": "provision", "needs_injection": True}
        )
        self.assertTrue(ok)

    def test_adb_is_scenario_agnostic(self) -> None:
        from aqa.adapters.device import AdbDevice

        ok, _ = AdbDevice("adb-1").supports({"scenario": "whatever"})
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
