"""产品生成器的容错行为测试。

对应真模型踩过的坑：
  - 一次性要整棵树 → JSON 被 max_tokens 截断，整轮降级
  - 截断/空内容不重试 → AI 写的代码永远进不了流水线
  - 上轮残留文件混进本轮构建产物
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aqa.generator import ApiProductGenerator
from aqa.llm import LLMError


class FakeModel:
    """按剧本返回内容的假模型，顺带记录每次调用的 token 预算。"""

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.budgets: list[int | None] = []

    def complete(self, system: str, prompt: str, max_tokens: int | None = None) -> str:
        self.budgets.append(max_tokens)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _file_json(code: str) -> str:
    return json.dumps({"content": code}, ensure_ascii=False)


class GeneratorTest(unittest.TestCase):
    def test_truncation_retries_with_bigger_budget(self) -> None:
        model = FakeModel(
            [
                json.dumps({"files": ["main.py", "panel/device.py"]}),
                _file_json("print('main')\n"),
                LLMError("输出被截断（finish_reason=length）"),
                _file_json("VERSION = '1.0.0'\n"),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            product = ApiProductGenerator(model, max_tokens=4096).generate("需求", Path(tmp))
            self.assertEqual(product.files, ["main.py", "panel/device.py"])
            self.assertTrue((Path(tmp) / "panel" / "device.py").exists())
        # 截断后预算翻倍重试
        self.assertEqual(model.budgets[1], 4096)
        self.assertEqual(model.budgets[2], 4096)
        self.assertEqual(model.budgets[3], 8192)

    def test_manifest_failure_degrades_to_single_file(self) -> None:
        model = FakeModel([RuntimeError("manifest down"), _file_json("print('solo')\n")])
        with tempfile.TemporaryDirectory() as tmp:
            product = ApiProductGenerator(model, max_tokens=4096).generate("需求", Path(tmp))
            self.assertEqual(product.files, ["main.py"])

    def test_stale_files_are_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "leftover.py"
            stale.write_text("old", encoding="utf-8")
            model = FakeModel([json.dumps({"files": ["main.py"]}), _file_json("print(1)\n")])
            ApiProductGenerator(model, max_tokens=4096).generate("需求", Path(tmp))
            self.assertFalse(stale.exists(), "上一轮残留文件必须清掉，否则会混进构建产物")

    def test_code_fence_is_stripped(self) -> None:
        model = FakeModel(
            [json.dumps({"files": ["main.py"]}), _file_json("```python\nprint('fenced')\n```")]
        )
        with tempfile.TemporaryDirectory() as tmp:
            ApiProductGenerator(model, max_tokens=4096).generate("需求", Path(tmp))
            content = (Path(tmp) / "main.py").read_text(encoding="utf-8")
            self.assertEqual(content, "print('fenced')")


if __name__ == "__main__":
    unittest.main()
