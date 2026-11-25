"""登录态管理测试：加密、扫码状态机、粘贴 Cookie、恢复与退出。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from harvester.auth import AuthManager, AuthError, Fernet
from harvester.constants import ErrorCode


class FakeStorage:
    def __init__(self) -> None:
        self.accounts: dict[int, dict] = {}
        self.status_calls: list[tuple[int, str]] = []

    async def latest_account(self):
        if not self.accounts:
            return None
        return sorted(
            self.accounts.values(), key=lambda row: row.get("last_ok_at", ""), reverse=True
        )[0]

    async def save_account(self, mid: int, uname: str, cookies_enc: str, source: str = "") -> None:
        self.accounts[mid] = {
            "mid": mid,
            "uname": uname,
            "cookies_enc": cookies_enc,
            "source": source,
            "status": "ok",
            "last_ok_at": "2026-01-01 00:00:00",
        }

    async def update_account_status(self, mid: int, status: str) -> None:
        self.status_calls.append((mid, status))
        if mid in self.accounts:
            self.accounts[mid]["status"] = status

    async def delete_account(self, mid: int) -> None:
        self.accounts.pop(mid, None)


class FakeClient:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.nav = {"isLogin": True, "mid": 1001, "uname": "测试用户"}
        self.qr = {"code": 86101, "message": "等待扫码", "url": "https://example.com/qr"}
        self.generated = {"url": "https://passport.bilibili.com/h5/qr?k=1", "qrcode_key": "KEY123"}

    def update_cookies(self, raw) -> None:
        if isinstance(raw, dict):
            self.cookies.update({str(k): str(v) for k, v in raw.items()})
        else:
            for part in str(raw).split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    self.cookies[key.strip()] = value.strip()

    def export_cookies(self) -> dict[str, str]:
        return dict(self.cookies)

    async def get_nav(self):
        return dict(self.nav)

    async def qr_generate(self):
        return dict(self.generated)

    async def qr_poll(self, key: str):
        return dict(self.qr)

    async def close(self) -> None:
        pass


def run(coro):
    return asyncio.run(coro)


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.storage = FakeStorage()
        self.client = FakeClient()
        self.auth = AuthManager(self.storage, self.client, self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_encrypt_roundtrip_and_key_file(self) -> None:
        cookies = {"SESSDATA": "abc123", "bili_jct": "xyz"}
        token = self.auth.encrypt_cookies(cookies)
        if Fernet is None:
            # 没装 cryptography 时降级为 b64（等价明文），不会生成密钥文件。
            # 断言必须跟着环境走，否则没有 cryptography 的 CI 会假红。
            self.assertTrue(token.startswith("b64:"))
            self.assertFalse(self.auth.key_path.exists())
        else:
            self.assertTrue(token.startswith("fernet:"))
            self.assertTrue(self.auth.key_path.exists())
        self.assertEqual(self.auth.decrypt_cookies(token), cookies)

    def test_decrypt_garbage_returns_empty(self) -> None:
        self.assertEqual(self.auth.decrypt_cookies("fernet:not-a-token"), {})
        self.assertEqual(self.auth.decrypt_cookies("b64:!!!not-base64!!!"), {})
        self.assertEqual(self.auth.decrypt_cookies(""), {})

    def test_save_cookie_requires_sessdata(self) -> None:
        with self.assertRaises(AuthError) as ctx:
            run(self.auth.save_cookie_text("bili_jct=1; DedeUserID=2"))
        self.assertEqual(ctx.exception.code, ErrorCode.LOGIN_REQUIRED)

    def test_save_cookie_success_persists(self) -> None:
        result = run(
            self.auth.save_cookie_text("SESSDATA=abc; bili_jct=jct; DedeUserID=1001")
        )
        self.assertTrue(result["status"])
        self.assertEqual(self.auth.state, "ok")
        self.assertIn(1001, self.storage.accounts)
        token = self.storage.accounts[1001]["cookies_enc"]
        self.assertEqual(self.auth.decrypt_cookies(token)["SESSDATA"], "abc")

    def test_save_cookie_rejected_when_nav_not_login(self) -> None:
        self.client.nav = {"isLogin": False}
        with self.assertRaises(AuthError):
            run(self.auth.save_cookie_text("SESSDATA=abc"))
        self.assertNotIn(1001, self.storage.accounts)

    def test_qr_flow_states(self) -> None:
        started = run(self.auth.start_qr())
        self.assertEqual(started["qrcode_key"], "KEY123")

        self.assertEqual(run(self.auth.poll_qr())["status"], "waiting")

        self.client.qr = {"code": 86090, "message": "已扫码"}
        self.assertEqual(run(self.auth.poll_qr("KEY123"))["status"], "scanned")

        self.client.qr = {"code": 86038, "message": "二维码已过期"}
        self.assertEqual(run(self.auth.poll_qr("KEY123"))["status"], "expired")

    def test_qr_success_finalizes_account(self) -> None:
        run(self.auth.start_qr())
        self.client.qr = {"code": 0, "message": "成功"}
        self.client.cookies = {"SESSDATA": "s", "bili_jct": "j"}
        result = run(self.auth.poll_qr("KEY123"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["account"]["mid"], 1001)
        self.assertIn(1001, self.storage.accounts)

    def test_poll_without_key_raises(self) -> None:
        with self.assertRaises(AuthError):
            run(self.auth.poll_qr(""))

    def test_restore_round_trip(self) -> None:
        run(self.auth.save_cookie_text("SESSDATA=abc; bili_jct=j"))
        other = AuthManager(self.storage, FakeClient(), self.data_dir)
        restored = run(other.restore())
        self.assertEqual(restored["mid"], 1001)
        self.assertEqual(other.client.cookies["SESSDATA"], "abc")

    def test_status_flattens_fields(self) -> None:
        run(self.auth.save_cookie_text("SESSDATA=abcdefgh; bili_jct=j"))
        data = run(self.auth.status())
        self.assertTrue(data["logged_in"])
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["mid"], 1001)
        self.assertEqual(data["uname"], "测试用户")
        self.assertNotIn("abcdefgh", data["masked"])

    def test_refresh_marks_expired(self) -> None:
        run(self.auth.save_cookie_text("SESSDATA=abc; bili_jct=j"))
        self.client.nav = {"isLogin": False}
        data = run(self.auth.refresh())
        self.assertFalse(data["logged_in"])
        self.assertEqual(data["status"], "expired")
        self.assertEqual(self.storage.status_calls[-1], (1001, "expired"))

    def test_refresh_empty_nav_keeps_unknown_without_persisting(self) -> None:
        """nav 返回空 dict（风控页被折成 {}）不能当成「已退出」写库。"""
        run(self.auth.save_cookie_text("SESSDATA=abc; bili_jct=j"))
        self.client.nav = {}
        data = run(self.auth.refresh())
        self.assertEqual(data["status"], "unknown")
        self.assertFalse(data["logged_in"])
        self.assertEqual(self.storage.status_calls, [])
        self.assertEqual(self.storage.accounts[1001]["status"], "ok")
        # 账号仍在，重启后 latest_account() 还能拿到 -> 不用重新扫码
        self.assertIsNotNone(run(self.storage.latest_account()))

    def test_logout_clears(self) -> None:
        run(self.auth.save_cookie_text("SESSDATA=abc; bili_jct=j"))
        run(self.auth.logout())
        self.assertEqual(self.auth.state, "none")
        self.assertEqual(self.client.cookies, {})
        self.assertEqual(self.storage.accounts, {})

    def test_poll_qr_without_code_stays_waiting(self) -> None:
        """轮询拿不到 code 时保持等待，既不判成功也不判过期。"""
        run(self.auth.start_qr())
        self.client.qr = {}
        result = run(self.auth.poll_qr())
        self.assertEqual(result["status"], "waiting")
        self.assertNotIn(1001, self.storage.accounts)


if __name__ == "__main__":
    unittest.main()
