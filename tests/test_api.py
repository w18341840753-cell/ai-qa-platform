"""Webhook 服务的安全行为测试。

默认 fail-closed：没配 secret 就拒绝；只有显式 AQA_ALLOW_OPEN_WEBHOOK=1 才开放。
另外覆盖投递去重、限流与 /runs 读接口鉴权。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient

    import aqa.api as api_mod

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


def _env(**overrides: str) -> dict:
    base = {
        "AQA_WEBHOOK_SECRET": "",
        "AQA_ALLOW_OPEN_WEBHOOK": "",
        "AQA_API_TOKEN": "",
        "AQA_RATE_LIMIT_PER_MIN": "20",
    }
    base.update(overrides)
    return base


@unittest.skipUnless(HAS_FASTAPI, "需要 fastapi（pip install fastapi uvicorn）")
class WebhookSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(api_mod.app)
        self.body = json.dumps(PAYLOAD).encode()
        # 后台任务是同步执行的，真跑一轮流水线会让测试挂住；这里只验证接入层行为
        self.exec_patch = patch("aqa.api._execute")
        self.exec_mock = self.exec_patch.start()
        self.addCleanup(self.exec_patch.stop)
        with api_mod._LOCK:
            api_mod.RUNS.clear()
            api_mod._RATE.clear()
            api_mod._DELIVERIES.clear()

    # ------------------------------------------------------------ 验签

    def test_fails_closed_without_secret(self) -> None:
        """默认更安全：没配 secret 时拒绝，而不是放行。"""
        with patch.dict(os.environ, _env(), clear=False):
            resp = self.client.post("/webhook/github", content=self.body)
            self.assertEqual(resp.status_code, 401)
            self.assertEqual(self.exec_mock.call_count, 0)
            health = self.client.get("/healthz")
            self.assertFalse(health.json()["signature_required"])
            self.assertFalse(health.json()["open_webhook"])

    def test_open_only_when_explicitly_allowed(self) -> None:
        """演示场景必须显式开 AQA_ALLOW_OPEN_WEBHOOK=1，且 /healthz 会如实暴露。"""
        with patch.dict(os.environ, _env(AQA_ALLOW_OPEN_WEBHOOK="1"), clear=False):
            resp = self.client.post("/webhook/github", content=self.body)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("accepted", resp.json())
            health = self.client.get("/healthz").json()
            self.assertFalse(health["signature_required"])
            self.assertTrue(health["open_webhook"])

    def test_rejects_missing_signature(self) -> None:
        with patch.dict(os.environ, _env(AQA_WEBHOOK_SECRET="s3cret"), clear=False):
            resp = self.client.post("/webhook/github", content=self.body)
            self.assertEqual(resp.status_code, 401)
            self.assertIn("signature", resp.json()["detail"])

    def test_rejects_forged_signature(self) -> None:
        """换一个 secret 算出来的签名必须被拒 —— 这是防住「随便谁都能触发」的关键。"""
        with patch.dict(os.environ, _env(AQA_WEBHOOK_SECRET="s3cret"), clear=False):
            forged = sign("wrong-secret", self.body)
            resp = self.client.post(
                "/webhook/github", content=self.body, headers={"X-Hub-Signature-256": forged}
            )
            self.assertEqual(resp.status_code, 401)

    def test_rejects_tampered_body(self) -> None:
        """签名对、body 被改过也必须拒。"""
        with patch.dict(os.environ, _env(AQA_WEBHOOK_SECRET="s3cret"), clear=False):
            good = sign("s3cret", self.body)
            tampered = json.dumps({**PAYLOAD, "after": "deadbeef"}).encode()
            resp = self.client.post(
                "/webhook/github", content=tampered, headers={"X-Hub-Signature-256": good}
            )
            self.assertEqual(resp.status_code, 401)

    def test_accepts_valid_signature(self) -> None:
        with patch.dict(os.environ, _env(AQA_WEBHOOK_SECRET="s3cret"), clear=False):
            good = sign("s3cret", self.body)
            resp = self.client.post(
                "/webhook/github", content=self.body, headers={"X-Hub-Signature-256": good}
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("accepted", resp.json())
            self.assertTrue(self.client.get("/healthz").json()["signature_required"])

    def test_rejects_garbage_json(self) -> None:
        with patch.dict(os.environ, _env(AQA_ALLOW_OPEN_WEBHOOK="1"), clear=False):
            resp = self.client.post("/webhook/github", content=b"not-json")
            self.assertEqual(resp.status_code, 400)

    # ------------------------------------------------------------ 重放 / 限流

    def test_duplicate_delivery_runs_once(self) -> None:
        """GitHub 重投同一个 X-GitHub-Delivery 时只跑一次。"""
        with patch.dict(os.environ, _env(AQA_ALLOW_OPEN_WEBHOOK="1"), clear=False):
            headers = {"X-GitHub-Delivery": "delivery-1"}
            first = self.client.post("/webhook/github", content=self.body, headers=headers)
            second = self.client.post("/webhook/github", content=self.body, headers=headers)
            self.assertEqual(first.status_code, 200)
            self.assertIn("accepted", first.json())
            self.assertTrue(second.json().get("duplicate"))
            self.assertEqual(self.exec_mock.call_count, 1)

    def test_rate_limit_blocks_burst(self) -> None:
        with patch.dict(
            os.environ,
            _env(AQA_ALLOW_OPEN_WEBHOOK="1", AQA_RATE_LIMIT_PER_MIN="2"),
            clear=False,
        ):
            codes = [
                self.client.post("/webhook/github", content=self.body).status_code
                for _ in range(3)
            ]
            self.assertEqual(codes, [200, 200, 429])
            self.assertEqual(self.exec_mock.call_count, 2)

    # ------------------------------------------------------------ 读接口鉴权

    def test_runs_requires_token_when_configured(self) -> None:
        with patch.dict(os.environ, _env(AQA_API_TOKEN="tok-123"), clear=False):
            self.assertEqual(self.client.get("/runs").status_code, 401)
            self.assertEqual(self.client.get("/runs/abc").status_code, 401)
            ok = self.client.get("/runs", headers={"X-AQA-Token": "tok-123"})
            self.assertEqual(ok.status_code, 200)
            self.assertIn("runs", ok.json())
            self.assertTrue(self.client.get("/healthz").json()["runs_auth_required"])

    def test_runs_open_when_no_token(self) -> None:
        """未配置令牌时保持兼容，但 /healthz 明确标注不要求鉴权。"""
        with patch.dict(os.environ, _env(), clear=False):
            self.assertEqual(self.client.get("/runs").status_code, 200)
            self.assertFalse(self.client.get("/healthz").json()["runs_auth_required"])


if __name__ == "__main__":
    unittest.main()
