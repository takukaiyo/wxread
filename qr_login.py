"""Server-side QR login flow for WeRead."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path


WEREAD_HOME = "https://weread.qq.com/"
READ_ENDPOINT = "https://weread.qq.com/web/book/read"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class QrLoginState:
    session_id: str
    status: str
    message: str
    image_path: Path | None = None
    curl_bash: str = ""
    saved: bool = False


class QrLoginManager:
    def __init__(self, data_dir: str | Path = "data", timeout_seconds: int = 180):
        self.data_dir = Path(data_dir)
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._state: QrLoginState | None = None

    def start(self) -> QrLoginState:
        with self._lock:
            session_id = secrets.token_urlsafe(12)
            image_path = self.data_dir / "qr-login.png"
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._state = QrLoginState(
                session_id=session_id,
                status="starting",
                message="Starting browser",
                image_path=image_path,
            )
            thread = threading.Thread(
                target=self._run_login,
                args=(session_id, image_path),
                name="wxread-qr-login",
                daemon=True,
            )
            thread.start()
            return self._state

    def status(self) -> QrLoginState:
        with self._lock:
            if self._state is None:
                return QrLoginState("", "idle", "No QR login session")
            return QrLoginState(**self._state.__dict__)

    def mark_saved(self) -> None:
        with self._lock:
            if self._state:
                self._state.saved = True

    def _set_state(self, session_id: str, **changes: object) -> None:
        with self._lock:
            if self._state is None or self._state.session_id != session_id:
                return
            for key, value in changes.items():
                setattr(self._state, key, value)

    def _run_login(self, session_id: str, image_path: Path) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self._set_state(
                session_id,
                status="error",
                message=(
                    "Playwright is not installed. Run: "
                    "uv run playwright install chromium"
                ),
            )
            return

        browser = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = browser.new_context(
                    viewport={"width": 430, "height": 760},
                    user_agent=DEFAULT_USER_AGENT,
                )
                page = context.new_page()
                page.goto(WEREAD_HOME, wait_until="networkidle", timeout=45000)
                page.get_by_text("登录", exact=True).last.click(timeout=10000)
                page.wait_for_selector("text=使用微信扫一扫登录", timeout=10000)
                qr_image = page.locator("img[src^='data:image']").first
                qr_image.wait_for(state="visible", timeout=10000)
                qr_image.screenshot(path=str(image_path))
                self._set_state(
                    session_id,
                    status="waiting",
                    message="请使用微信扫描二维码",
                    image_path=image_path,
                )

                deadline = time.time() + self.timeout_seconds
                while time.time() < deadline:
                    cookies = context.cookies(WEREAD_HOME)
                    names = {cookie["name"] for cookie in cookies}
                    if "wr_vid" in names or "wr_skey" in names:
                        curl_bash = build_curl_from_cookies(cookies, DEFAULT_USER_AGENT)
                        self._set_state(
                            session_id,
                            status="success",
                            message="QR login completed",
                            curl_bash=curl_bash,
                        )
                        return
                    if page.is_closed():
                        self._set_state(session_id, status="error", message="Login page closed")
                        return
                    time.sleep(2)
                self._set_state(session_id, status="expired", message="QR login timed out")
        except Exception as exc:
            self._set_state(session_id, status="error", message=str(exc))
        finally:
            if browser:
                browser.close()


def build_curl_from_cookies(cookies: list[dict[str, object]], user_agent: str) -> str:
    cookie_pairs: list[str] = []
    for cookie in cookies:
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", "")).strip()
        if name and value:
            cookie_pairs.append(f"{name}={value}")
    cookie_header = "; ".join(cookie_pairs)
    return (
        f"curl '{READ_ENDPOINT}' "
        "-H 'accept: application/json, text/plain, */*' "
        "-H 'content-type: application/json;charset=UTF-8' "
        f"-H 'user-agent: {user_agent}' "
        "-H 'referer: https://weread.qq.com/' "
        f"-b '{cookie_header}'"
    )
