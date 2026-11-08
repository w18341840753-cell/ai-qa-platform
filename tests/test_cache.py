"""按 commit 产物缓存的回归测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aqa.cache import ArtifactCache
from aqa.config import Settings
from aqa.context import RunContext
from aqa.stages import PlanStage, ProductStage


def _run_ctx(root: Path, run_id: str, commit: str, trigger: dict | None = None) -> RunContext:
    ctx = RunContext(
        run_id=run_id,
        repo="org/repo",
        commit=commit,
        artifacts=root / "runs" / run_id,
    )
    if trigger is not None:
        ctx.data["trigger"] = trigger
    return ctx


class ArtifactCacheTest(unittest.TestCase):
    def test_same_key_hits_with_same_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = ArtifactCache(Settings(workspace=root))
            self.assertEqual(cache.root, root / ".aqa" / "cache")

            key = cache.key("repo", "abc", "req", "model")
            cache.put_json("plan", key, {"cases": [{"id": "TC-01"}]})
            self.assertEqual(cache.get_json("plan", key), {"cases": [{"id": "TC-01"}]})

            src = root / "src"
            src.mkdir()
            (src / "main.py").write_text("print('ok')", encoding="utf-8")
            cache.put_dir("product", key, src)
            dest = root / "restored"
            self.assertTrue(cache.get_dir("product", key, dest))
            self.assertEqual((dest / "main.py").read_text(encoding="utf-8"), "print('ok')")

    def test_different_key_misses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ArtifactCache(Settings(workspace=Path(tmp)))
            cache.put_json("plan", cache.key("org/repo", "aaa"), {"commit": "aaa"})
            self.assertIsNone(cache.get_json("plan", cache.key("org/repo", "bbb")))

    def test_disabled_cache_never_reads_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            enabled = ArtifactCache(Settings(workspace=root))
            key = enabled.key("existing")
            enabled.put_json("plan", key, {"ok": True})

            disabled = ArtifactCache(Settings(workspace=root, cache_enabled=False))
            self.assertIsNone(disabled.get_json("plan", key))

            new_key = disabled.key("new")
            disabled.put_json("plan", new_key, {"ok": True})
            self.assertFalse((enabled.root / "plan" / f"{new_key}.json").exists())

            src = root / "src"
            src.mkdir()
            disabled.put_dir("product", new_key, src)
            self.assertFalse((enabled.root / "product" / new_key).exists())

    def test_namespace_evicts_oldest_after_twenty_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ArtifactCache(Settings(workspace=Path(tmp)))
            for idx in range(21):
                key = f"k{idx:02d}"
                cache.put_json("plan", key, {"idx": idx})
                os.utime(cache.root / "plan" / f"{key}.json", (1000 + idx, 1000 + idx))

            files = {path.stem for path in (cache.root / "plan").glob("*.json")}
            self.assertEqual(len(files), 20)
            self.assertNotIn("k00", files)
            self.assertIn("k20", files)

    def test_io_failure_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = ArtifactCache(Settings(workspace=root))
            key = cache.key("io")

            with mock.patch.object(Path, "mkdir", side_effect=PermissionError("read-only")):
                cache.put_json("plan", key, {"ok": True})
            self.assertIsNone(cache.get_json("plan", key))

            src = root / "src"
            src.mkdir()
            (src / "main.py").write_text("print('ok')", encoding="utf-8")
            with mock.patch("aqa.cache.shutil.copytree", side_effect=OSError("read-only")):
                cache.put_dir("product", key, src)
                self.assertFalse(cache.get_dir("product", key, root / "restored"))


class _FakeProduct:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.files = ["main.py"]
        self.generator = "fake"

    def to_dict(self) -> dict:
        return {
            "name": "fake-product",
            "path": str(self.path),
            "files": list(self.files),
            "generator": self.generator,
        }


class _CountingGenerator:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, requirement: str, out_dir: Path) -> _FakeProduct:
        self.calls += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "main.py").write_text("print('ok')", encoding="utf-8")
        return _FakeProduct(out_dir)


_CHECK_OK = {
    "ok": True,
    "summary": "ok",
    "command": "python main.py",
    "exit_code": 0,
    "missing": [],
    "state": {},
}


class ProductStageCacheTest(unittest.TestCase):
    def _settings(self, root: Path, **kw) -> Settings:
        return Settings(
            workspace=root,
            product_requirement="req",
            text_model="model",
            case_timeout=3,
            **kw,
        )

    def test_hit_skips_generation_and_still_probes_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generator = _CountingGenerator()
            rt = SimpleNamespace(
                settings=self._settings(root),
                models=SimpleNamespace(mode="mock"),
            )
            with mock.patch("aqa.stages.build_generator", return_value=generator), mock.patch(
                "aqa.stages.contract.probe", return_value=dict(_CHECK_OK)
            ) as probe:
                first = ProductStage().run(_run_ctx(root, "r1", "abc"), rt)
                second = ProductStage().run(_run_ctx(root, "r2", "abc"), rt)

            self.assertNotIn("cached", first)
            self.assertTrue(second["cached"])
            self.assertEqual(second["path"], str(root / "runs" / "r2" / "product"))
            self.assertTrue((root / "runs" / "r2" / "product" / "main.py").is_file())
            self.assertEqual(generator.calls, 1)
            self.assertEqual(probe.call_count, 2, "缓存命中也要重新验收契约")

    def test_failed_contract_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generator = _CountingGenerator()
            rt = SimpleNamespace(
                settings=self._settings(root),
                models=SimpleNamespace(mode="mock"),
            )
            bad = dict(_CHECK_OK, ok=False, summary="missing main.py")
            with mock.patch("aqa.stages.build_generator", return_value=generator), mock.patch(
                "aqa.stages.contract.probe", return_value=bad
            ):
                ProductStage().run(_run_ctx(root, "r1", "abc"), rt)
                ProductStage().run(_run_ctx(root, "r2", "abc"), rt)

            self.assertEqual(generator.calls, 2, "坏产物不能固化成缓存")

    def test_disabled_cache_still_generates_every_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generator = _CountingGenerator()
            rt = SimpleNamespace(
                settings=self._settings(root, cache_enabled=False),
                models=SimpleNamespace(mode="mock"),
            )
            with mock.patch("aqa.stages.build_generator", return_value=generator), mock.patch(
                "aqa.stages.contract.probe", return_value=dict(_CHECK_OK)
            ):
                ProductStage().run(_run_ctx(root, "r1", "abc"), rt)
                second = ProductStage().run(_run_ctx(root, "r2", "abc"), rt)

            self.assertEqual(generator.calls, 2)
            self.assertNotIn("cached", second)


_PLAN = {
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
}


class _CountingText:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def complete(self, system: str, prompt: str, max_tokens: int | None = None) -> str:
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


class _Device:
    def capabilities(self) -> dict:
        return {"scenarios": ["boot", "provision", "ota", "reconnect"], "injection": True}


class PlanStageCacheTest(unittest.TestCase):
    def _rt(self, root: Path) -> tuple[SimpleNamespace, _CountingText]:
        text = _CountingText(_PLAN)
        rt = SimpleNamespace(
            settings=Settings(workspace=root, text_model="model"),
            models=SimpleNamespace(text=text),
            device=_Device(),
        )
        return rt, text

    def test_hit_skips_model_and_ignores_changed_file_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rt, text = self._rt(root)
            PlanStage().run(
                _run_ctx(root, "r1", "abc", {"repo": "org/repo", "commit": "abc", "changed_files": ["b", "a"]}),
                rt,
            )
            second = PlanStage().run(
                _run_ctx(root, "r2", "abc", {"repo": "org/repo", "commit": "abc", "changed_files": ["a", "b"]}),
                rt,
            )

            self.assertTrue(second["cached"])
            self.assertEqual(second["cases"][0]["id"], "TC-01")
            self.assertEqual(text.calls, 1)

    def test_new_commit_misses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rt, text = self._rt(root)
            PlanStage().run(
                _run_ctx(root, "r1", "aaa", {"repo": "org/repo", "commit": "aaa", "changed_files": ["a"]}),
                rt,
            )
            second = PlanStage().run(
                _run_ctx(root, "r2", "bbb", {"repo": "org/repo", "commit": "bbb", "changed_files": ["a"]}),
                rt,
            )

            self.assertEqual(text.calls, 2)
            self.assertNotIn("cached", second)


if __name__ == "__main__":
    unittest.main()
