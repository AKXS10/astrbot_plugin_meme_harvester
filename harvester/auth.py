"""B 站登录态管理：扫码登录、Cookie 粘贴、加密持久化。

设计要点：
- 不保存任何"长效 auth_key"（B 站 web 端没有），只保存登录后的完整 cookie 集；
- cookie 使用 Fernet 对称加密后落库，密钥文件与数据库同目录且不随插件分发；
- 每次请求后由 BiliClient 自动合并 Set-Cookie，因此 SESSDATA 续期会被带回。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

from .bili.client import BiliClient, BiliError
from .bili.cookies import mask_cookie, parse_cookie_string
from .constants import ErrorCode

try:  # pragma: no cover - 取决于运行环境
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # pragma: no cover
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]

QR_SUCCESS = 0
QR_WAITING = 86101
QR_SCANNED = 86090
QR_EXPIRED = 86038

#: 扫码二维码 3 分钟有效，留出轮询余量
QR_TTL_SECONDS = 180


class AuthError(Exception):
    """登录流程异常，携带给前端的状态码。"""

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        self.code = code
        super().__init__(message or code.value)


class AuthManager:
    def __init__(
        self,
        storage,
        client: BiliClient,
        data_dir: Path | str,
        logger=None,
    ) -> None:
        self.storage = storage
        self.client = client
        self.data_dir = Path(data_dir)
        self.logger = logger
        self._fernet: Any = None
        self._fernet_ready = False
        self._lock = asyncio.Lock()
        self._pending_key = ""
        self._pending_at = 0.0
        self._last_verify = 0.0
        self._verify_ttl = 20.0
        self.account: dict[str, Any] | None = None
        self.state = "unknown"  # unknown / ok / expired / none

    # ------------------------------------------------------------ 加密

    @property
    def key_path(self) -> Path:
        return self.data_dir / "secret.key"

    def _ensure_fernet(self) -> Any:
        if self._fernet_ready:
            return self._fernet
        if Fernet is None:
            # 缺少 cryptography 时只能存 base64（等价明文），必须明确告诉用户，
            # 而不是静默降级。
            if self.logger:
                self.logger.warning(
                    "缺少 cryptography，B 站 cookie 将以 base64 明文保存；"
                    "安装 requirements.txt 后重新登录即可恢复加密存储"
                )
            self._fernet = None
            self._fernet_ready = True
            return None
        try:
            if not self.key_path.exists():
                self.data_dir.mkdir(parents=True, exist_ok=True)
                key = Fernet.generate_key()
                self.key_path.write_bytes(key)
                try:
                    self.key_path.chmod(0o600)
                except OSError:
                    pass
            self._fernet = Fernet(self.key_path.read_bytes().strip())
        except Exception as exc:  # pragma: no cover - 磁盘/权限异常
            # 不置 _fernet_ready：下次调用重试，避免一次瞬时磁盘错误永久降级成明文。
            if self.logger:
                self.logger.warning("无法初始化 cookie 加密密钥：%s", exc)
            self._fernet = None
            return None
        self._fernet_ready = True
        return self._fernet

    def encrypt_cookies(self, cookies: dict[str, str]) -> str:
        payload = json.dumps(cookies, ensure_ascii=False, sort_keys=True).encode("utf-8")
        fernet = self._ensure_fernet()
        if fernet is not None:
            return "fernet:" + fernet.encrypt(payload).decode("ascii")
        return "b64:" + base64.b64encode(payload).decode("ascii")

    def decrypt_cookies(self, token: str) -> dict[str, str]:
        if not token:
            return {}
        try:
            if token.startswith("fernet:"):
                fernet = self._ensure_fernet()
                if fernet is None:
                    return {}
                raw = fernet.decrypt(token[len("fernet:") :].encode("ascii"))
            elif token.startswith("b64:"):
                raw = base64.b64decode(token[len("b64:") :])
            else:
                return {}
            data = json.loads(raw.decode("utf-8"))
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (InvalidToken, ValueError, KeyError):
            if self.logger:
                self.logger.warning("已保存的 B 站 cookie 无法解密，请重新登录")
            return {}

    # ------------------------------------------------------------ 状态

    def _public_account(self) -> dict[str, Any] | None:
        if not self.account:
            return None
        return {
            "mid": int(self.account.get("mid") or 0),
            "uname": str(self.account.get("uname") or ""),
            "source": str(self.account.get("source") or ""),
            "status": self.state,
            "last_ok_at": str(self.account.get("last_ok_at") or ""),
        }

    async def restore(self) -> dict[str, Any] | None:
        """启动时恢复最近一次有效登录。"""
        async with self._lock:
            row = await self.storage.latest_account()
            if not row:
                self.state = "none"
                return None
            cookies = self.decrypt_cookies(str(row.get("cookies_enc") or ""))
            if not cookies:
                await self.storage.update_account_status(int(row.get("mid") or 0), "expired")
                self.state = "expired"
                return None
            self.client.update_cookies(cookies)
            self.account = row
            self.state = "unknown"
            return self._public_account()

    async def status(self, *, verify: bool = False) -> dict[str, Any]:
        if self.account is None and self.state in ("unknown", "none"):
            await self.restore()
        # 重启后 Cookie 已恢复但尚未校验：自动打一次 nav，避免 UI 一直显示「未登录」
        if (
            self.account is not None
            and self.state == "unknown"
            and time.monotonic() - self._last_verify > self._verify_ttl
        ):
            return await self.refresh()
        if verify and self.account is not None:
            return await self.refresh()
        acct = self._public_account() or {}
        masked = self.describe_cookies()
        return {
            "logged_in": self.state == "ok",
            "status": self.state,
            "mid": acct.get("mid") or 0,
            "uname": acct.get("uname") or "",
            "source": acct.get("source") or "",
            "last_ok_at": acct.get("last_ok_at") or "",
            "cookie_count": len(self.client.cookies),
            "has_secret_key": self.key_path.exists(),
            "masked": masked.get("SESSDATA", ""),
        }

    async def refresh(self) -> dict[str, Any]:
        """调用 nav 校验登录态；-101/-352 时标记失效。"""
        self._last_verify = time.monotonic()
        if not self.client.cookies:
            self.state = "none"
            return await self.status()
        try:
            nav = await self.client.get_nav()
        except BiliError as exc:
            if exc.code in (ErrorCode.LOGIN_EXPIRED, ErrorCode.LOGIN_REQUIRED):
                self.state = "expired"
                if self.account:
                    await self.storage.update_account_status(int(self.account.get("mid") or 0), "expired")
            else:
                self.state = "unknown"
                if self.logger:
                    self.logger.warning("nav 校验失败（%s），登录态暂时未知：%s", exc.code.value, exc)
            return await self.status()
        if not nav:
            # 空响应（风控 HTML 页被折成 {}、网关占位响应等）无法判定登录态：
            # 保持 unknown 且绝不落库，否则 latest_account() 只认 ok，
            # 重启后会表现为「账号消失，必须重新扫码」。
            self.state = "unknown"
            if self.logger:
                self.logger.warning("nav 返回空响应，无法判定登录态，保持 unknown 且不改动已保存账号")
            return await self.status()
        if not nav.get("isLogin"):
            self.state = "expired"
            if self.account:
                await self.storage.update_account_status(int(self.account.get("mid") or 0), "expired")
            return await self.status()
        mid = int(nav.get("mid") or 0)
        uname = str(nav.get("uname") or "")
        if mid:
            if not self.account or int(self.account.get("mid") or 0) != mid:
                self.account = {"mid": mid, "uname": uname, "source": "refresh", "last_ok_at": ""}
            else:
                self.account["uname"] = uname or self.account.get("uname", "")
        self.state = "ok"
        return await self.status()

    # ------------------------------------------------------------ 扫码

    async def start_qr(self) -> dict[str, Any]:
        data = await self.client.qr_generate()
        if not data.get("url") or not data.get("qrcode_key"):
            raise AuthError(ErrorCode.INTERNAL, "无法获取登录二维码")
        self._pending_key = str(data["qrcode_key"])
        self._pending_at = time.monotonic()
        return {
            "url": data["url"],
            "qrcode_key": self._pending_key,
            "expires_in": QR_TTL_SECONDS,
        }

    async def poll_qr(self, qrcode_key: str = "") -> dict[str, Any]:
        key = qrcode_key or self._pending_key
        if not key:
            raise AuthError(ErrorCode.LOGIN_REQUIRED, "请先获取二维码")
        if self._pending_at and time.monotonic() - self._pending_at > QR_TTL_SECONDS + 60:
            self._pending_key = ""
            return {"status": "expired", "message": "二维码已过期，请刷新"}
        data = await self.client.qr_poll(key)
        try:
            code = int(data.get("code"))
        except (TypeError, ValueError):
            code = -1
        if code < 0:
            # 没拿到明确状态（空响应/换了字段）时既不能判成功也不能判过期，
            # 保持等待让前端继续轮询。
            return {"status": "waiting", "message": data.get("message") or "等待扫码"}
        if code == QR_SUCCESS:
            account = await self._finalize("qrcode")
            self._pending_key = ""
            return {"status": "ok", "message": "登录成功", "account": account}
        if code == QR_SCANNED:
            return {"status": "scanned", "message": "已扫码，请在手机上确认"}
        if code == QR_WAITING:
            return {"status": "waiting", "message": "等待扫码"}
        if code == QR_EXPIRED:
            self._pending_key = ""
            return {"status": "expired", "message": "二维码已过期，请刷新"}
        return {"status": "error", "message": data.get("message") or f"登录失败（{code}）"}

    # ------------------------------------------------------------ 粘贴

    async def save_cookie_text(self, raw: str) -> dict[str, Any]:
        if len(raw) > 20_000:
            # 防御性上限：正常浏览器 Cookie 头不会超过几 KB，超长多半是误粘整页内容
            raise AuthError(ErrorCode.BAD_URL, "Cookie 内容过长（>20000 字符），请只粘贴请求头里的 Cookie 值")
        cookies = parse_cookie_string(raw)
        if not cookies:
            raise AuthError(ErrorCode.BAD_URL, "Cookie 格式无法解析，请粘贴完整 Cookie 字符串")
        if "SESSDATA" not in cookies:
            raise AuthError(ErrorCode.LOGIN_REQUIRED, "Cookie 里缺少 SESSDATA，请从已登录的浏览器复制完整 Cookie")
        self.client.update_cookies(cookies)
        account = await self._finalize("cookie")
        return {"status": "ok", "message": "Cookie 已保存", "account": account}

    async def _finalize(self, source: str) -> dict[str, Any]:
        nav = await self.client.get_nav()
        if not nav.get("isLogin"):
            raise AuthError(ErrorCode.LOGIN_REQUIRED, "登录态无效，请重新扫码")
        mid = int(nav.get("mid") or 0)
        uname = str(nav.get("uname") or "")
        if not mid:
            raise AuthError(ErrorCode.LOGIN_REQUIRED, "登录态缺少 UID，请重新扫码")
        cookies = self.client.export_cookies()
        token = self.encrypt_cookies(cookies)
        await self.storage.save_account(mid=mid, uname=uname, cookies_enc=token, source=source)
        self.account = {
            "mid": mid,
            "uname": uname,
            "source": source,
            "last_ok_at": "",
        }
        self.state = "ok"
        if self.logger:
            self.logger.info(
                "B 站登录成功：%s(%s)，cookie 数 %d，来源 %s",
                uname,
                mid,
                len(cookies),
                source,
            )
        return self._public_account() or {}

    async def logout(self) -> None:
        if self.account:
            await self.storage.delete_account(int(self.account.get("mid") or 0))
        self.account = None
        self.client.cookies.clear()
        self.state = "none"

    def describe_cookies(self) -> dict[str, str]:
        return mask_cookie(self.client.cookies)
