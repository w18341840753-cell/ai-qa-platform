"""构建适配器测试：打包只收源码，不把平台产物卷进去。

历史事故：webhook 模式没配 AQA_SOURCE_DIR，构建以工作目录为源码，
把 3.9GB 的 runs/ 压成 1.7GB 的 firmware.zip，整条流水线卡死。
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from aqa.adapters.build import PackageBuild, SKIP_DIRS


class PackageBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _write(self, rel: str, content: str = "x") -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_only_source_files_are_packed(self) -> None:
        self._write("main.py", "print('hi')")
        self._write("panel/device.py")
        # 这些都应该被排除
        self._write("runs/20260101-000000-abcdef/firmware.zip", "x" * 5000)
        self._write(".git/config")
        self._write(".env")
        self._write(".aqa/bugs.json")
        self._write("__pycache__/main.cpython-313.pyc")
        self._write("node_modules/lib/index.js")

        out = self.root / "out"
        result = PackageBuild().build(self.root, out)

        with zipfile.ZipFile(result["path"]) as zf:
            names = set(zf.namelist())

        self.assertEqual(names, {"main.py", "panel/device.py"})
        # 6 个噪声文件 + 输出目录里的 firmware.zip 自己（不能边写边压）
        self.assertEqual(result["skipped"], 7)
        self.assertIn("runs", SKIP_DIRS)

    def test_artifact_limit_aborts_instead_of_filling_disk(self) -> None:
        self._write("main.py")
        self._write("big.bin", "0" * (3 * 1024 * 1024))  # 3MB

        out = self.root / "out"
        with self.assertRaises(RuntimeError) as ctx:
            PackageBuild(max_mb=1).build(self.root, out)

        msg = str(ctx.exception)
        self.assertIn("上限", msg)
        self.assertIn("AQA_SOURCE_DIR", msg)
        # 不能留下半成品，否则下游会拿到一个坏固件
        self.assertFalse((out / "firmware.zip").exists())

    def test_limit_is_configurable(self) -> None:
        self._write("main.py")
        result = PackageBuild(max_mb=100).build(self.root, self.root / "out")
        self.assertEqual(result["files"], ["main.py"])

    def test_missing_dir_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            PackageBuild().build(self.root / "nope", self.root / "out")


if __name__ == "__main__":
    unittest.main()
