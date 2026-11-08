"""Webhook 服务的安全行为测试：没配 secret 时开放，配了就必须验签。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient

    from aqa.api import app

    HAS_FASTAPI = True
except Exception:  # noqa: BLE001 - 未安装可选依赖时整组跳过
    HAS_FASTAPI = False

PAYLOAD = {
    "ref": "refs/heads/main",
    "repository": {"full_name": "demo/panel", "clone_url": "https://example.com/x.git"},
    "after": "abc1234",
    "head_commit": {"modified": ["src/net/provision.c"]},
}


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@unittest.skipUnless(HAS_FASTAPI, "需要 fastapi（pip install fastapi uvicorn）")
class WebhookSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.body = json.dumps(PAYLOAD).encode()
        # 后台任务是同步执行的，真跑一轮流水线会让测试挂住；这里只验证接入层行为
        self.exec_patch = patch("aqa.api._execute")
        self.exec_patch.start()
        self.addCleanup(self.exec_patch.stop)

    def test_open_when_no_secret(self) -> None:
        """未配置 secret 时保持兼容：可触发，但 /healthz 明确告知是开放的。"""
        with patch.dict(os.environ, {"AQA_WEBHOOK_SECRET": ""}, clear=False):
            resp = self.client.post("/webhook/github", content=self.body)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("accepted", resp.json())
            health = self.client.get("/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertFalse(health.json()["signature_required"])

    def test_rejects_missing_signature(self) -> None:
        with patch.dict(os.environ, {"AQA_WEBHOOK_SECRET": "s3cret"}, clear=False):
            resp = self.client.post("/webhook/github", content=self.body)
            self.assertEqual(resp.status_code, 401)
            self.assertIn("signature", resp.json()["detail"])

    def test_rejects_forged_signature(self) -> None:
        """换一个 secret 算出来的签名必须被拒 —— 这是防住「随便谁都能触发」的关键。"""
        with patch.dict(os.environ, {"AQA_WEBHOOK_SECRET": "s3cret"}, clear=False):
            forged = sign("wrong-secret", self.body)
            resp = self.client.post(
                "/webhook/github", content=self.body, headers={"X-Hub-Signature-256": forged}
            )
            self.assertEqual(resp.status_code, 401)

    def test_rejects_tampered_body(self) -> None:
        """签名对、body 被改过也必须拒。"""
        with patch.dict(os.environ, {"AQA_WEBHOOK_SECRET": "s3cret"}, clear=False):
            good = sign("s3cret", self.body)
            tampered = json.dumps({**PAYLOAD, "after": "deadbeef"}).encode()
            resp = self.client.post(
                "/webhook/github", content=tampered, headers={"X-Hub-Signature-256": good}
            )
            self.assertEqual(resp.status_code, 401)

    def test_accepts_valid_signature(self) -> None:
        with patch.dict(os.environ, {"AQA_WEBHOOK_SECRET": "s3cret"}, clear=False):
            good = sign("s3cret", self.body)
            resp = self.client.post(
                "/webhook/github", content=self.body, headers={"X-Hub-Signature-256": good}
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("accepted", resp.json())
            self.assertTrue(self.client.get("/healthz").json()["signature_required"])

    def test_rejects_garbage_json(self) -> None:
        with patch.dict(os.environ, {"AQA_WEBHOOK_SECRET": ""}, clear=False):
            resp = self.client.post("/webhook/github", content=b"not-json")
            self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
