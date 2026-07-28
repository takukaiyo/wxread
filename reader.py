"""Reusable wxread request logic for CLI and web control panel."""

from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import shlex
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from config import book as DEFAULT_BOOKS
from config import chapter as DEFAULT_CHAPTERS
from config import cookies as DEFAULT_COOKIES
from config import data as DEFAULT_DATA
from config import headers as DEFAULT_HEADERS
from config import READ_NUM as DEFAULT_READ_NUM
from config import PUSH_METHOD as DEFAULT_PUSH_METHOD
from config import PUSHPLUS_TOKEN as DEFAULT_PUSHPLUS_TOKEN
from config import SERVERCHAN_SPT as DEFAULT_SERVERCHAN_SPT
from config import TELEGRAM_BOT_TOKEN as DEFAULT_TELEGRAM_BOT_TOKEN
from config import TELEGRAM_CHAT_ID as DEFAULT_TELEGRAM_CHAT_ID
from config import WXPUSHER_SPT as DEFAULT_WXPUSHER_SPT
from push import PushSettings, push


KEY = "3c5c8717f3daf09iop3423zafeqoi"
COOKIE_DATA = {"rq": "%2Fweb%2Fbook%2Fread"}
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"
SEARCH_URL = "https://weread.qq.com/web/search/global"
WEREAD_HOME = "https://weread.qq.com/"
REQUEST_TIMEOUT = 15
BROWSER_RESPONSE_TIMEOUT_MS = 20_000
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class ParsedCurl:
    headers: dict[str, str]
    cookies: dict[str, str]


@dataclass
class ReaderConfig:
    read_num: int = DEFAULT_READ_NUM
    push_method: str = DEFAULT_PUSH_METHOD or ""
    pushplus_token: str = DEFAULT_PUSHPLUS_TOKEN or ""
    telegram_bot_token: str = DEFAULT_TELEGRAM_BOT_TOKEN or ""
    telegram_chat_id: str = DEFAULT_TELEGRAM_CHAT_ID or ""
    wxpusher_spt: str = DEFAULT_WXPUSHER_SPT or ""
    serverchan_spt: str = DEFAULT_SERVERCHAN_SPT or ""
    headers: dict[str, str] = field(default_factory=lambda: copy.deepcopy(DEFAULT_HEADERS))
    cookies: dict[str, str] = field(default_factory=lambda: copy.deepcopy(DEFAULT_COOKIES))
    data: dict[str, object] = field(default_factory=lambda: copy.deepcopy(DEFAULT_DATA))
    books: list[str] = field(default_factory=lambda: list(DEFAULT_BOOKS))
    chapters: list[str] = field(default_factory=lambda: list(DEFAULT_CHAPTERS))
    selected_book_infos: list[BookInfo] = field(default_factory=list)


@dataclass
class ReaderEvent:
    level: str
    message: str


@dataclass
class ReaderResult:
    status: str
    success_count: int
    error: str | None = None


@dataclass
class LoginTestResult:
    ok: bool
    message: str


@dataclass
class BookInfo:
    book_id: str
    title: str
    author: str = ""
    cover: str = ""


ProgressCallback = Callable[[ReaderEvent], None]
CancelCheck = Callable[[], bool]


def parse_curl_command(curl_command: str) -> ParsedCurl:
    if not curl_command or not curl_command.strip():
        raise ValueError("curl 内容不能为空")

    headers: dict[str, str] = {}
    cookie_string = ""
    try:
        parts = shlex.split(curl_command)
    except ValueError as exc:
        raise ValueError(f"curl 内容无法解析：{exc}") from exc

    index = 0
    while index < len(parts):
        token = parts[index]
        next_value = parts[index + 1] if index + 1 < len(parts) else ""
        if token in {"-H", "--header"} and next_value:
            if ":" in next_value:
                key, value = next_value.split(":", 1)
                key = key.strip()
                value = value.strip()
                if key.lower() == "cookie":
                    cookie_string = value
                else:
                    headers[key] = value
            index += 2
            continue
        if token in {"-b", "--cookie", "--cookie-jar"} and next_value:
            cookie_string = next_value.strip()
            index += 2
            continue
        index += 1

    # Fallback for curl strings copied with unusual quoting.
    if not cookie_string:
        cookie_match = re.search(r"(?:Cookie|cookie):\s*([^'\"]+)", curl_command)
        if cookie_match:
            cookie_string = cookie_match.group(1).strip()

    cookies = parse_cookie_string(cookie_string)
    if not cookies:
        raise ValueError("curl 内容里没有可用的 Cookie")
    return ParsedCurl(headers=headers, cookies=cookies)


def parse_cookie_string(cookie_string: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in cookie_string.split(";"):
        if "=" not in cookie:
            continue
        key, value = cookie.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            cookies[key] = value
    return cookies


def build_config_from_settings(settings: dict[str, str]) -> ReaderConfig:
    config = ReaderConfig(
        read_num=int(settings.get("READ_NUM") or DEFAULT_READ_NUM),
        push_method=settings.get("PUSH_METHOD", "") or "",
        pushplus_token=settings.get("PUSHPLUS_TOKEN", "") or "",
        telegram_bot_token=settings.get("TELEGRAM_BOT_TOKEN", "") or "",
        telegram_chat_id=settings.get("TELEGRAM_CHAT_ID", "") or "",
        wxpusher_spt=settings.get("WXPUSHER_SPT", "") or "",
        serverchan_spt=settings.get("SERVERCHAN_SPT", "") or "",
    )
    curl_bash = settings.get("WXREAD_CURL_BASH", "")
    if curl_bash:
        parsed = parse_curl_command(curl_bash)
        config.headers = parsed.headers
        config.cookies = parsed.cookies
    selected_books = parse_selected_books(settings.get("SELECTED_BOOKS", ""))
    if selected_books:
        config.books = selected_books
        selected_set = set(selected_books)
        config.selected_book_infos = [
            book
            for book in parse_book_library(settings.get("BOOK_LIBRARY", ""))
            if book.book_id in selected_set
        ]
    return config


def parse_selected_books(value: str) -> list[str]:
    books: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[\s,;]+", value or ""):
        book_id = raw.strip()
        if not book_id or book_id in seen:
            continue
        books.append(book_id)
        seen.add(book_id)
    return books


def serialize_book_library(books: list[BookInfo]) -> str:
    compact = [
        {
            "bookId": book.book_id,
            "title": book.title,
            "author": book.author,
            "cover": book.cover,
        }
        for book in books
        if is_valid_book_id(book.book_id)
    ]
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def parse_book_library(value: str) -> list[BookInfo]:
    if not value:
        return []
    try:
        raw_books = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw_books, list):
        return []

    books: list[BookInfo] = []
    seen: set[str] = set()
    for raw in raw_books:
        if not isinstance(raw, dict):
            continue
        book_id = str(raw.get("bookId") or raw.get("book_id") or "").strip()
        if not is_valid_book_id(book_id) or book_id in seen:
            continue
        title = str(raw.get("title") or book_id).strip()
        author = str(raw.get("author") or "").strip()
        cover = str(raw.get("cover") or "").strip()
        books.append(BookInfo(book_id=book_id, title=title, author=author, cover=cover))
        seen.add(book_id)
    return books


def is_valid_book_id(book_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]+", book_id or ""))


def search_weread_books(keyword: str, limit: int = 20) -> list[BookInfo]:
    query = (keyword or "").strip()
    if not query:
        return []
    response = requests.get(SEARCH_URL, params={"keyword": query}, timeout=10)
    response.raise_for_status()
    data = response.json()
    results: list[BookInfo] = []
    seen: set[str] = set()
    for item in data.get("books", []):
        info = item.get("bookInfo") if isinstance(item, dict) else None
        if not isinstance(info, dict):
            continue
        book_id = str(info.get("bookId") or "").strip()
        title = str(info.get("title") or "").strip()
        if not is_valid_book_id(book_id) or not title or book_id in seen:
            continue
        results.append(
            BookInfo(
                book_id=book_id,
                title=title,
                author=str(info.get("author") or "").strip(),
                cover=str(info.get("cover") or "").strip(),
            )
        )
        seen.add(book_id)
        if len(results) >= limit:
            break
    return results


def emit(callback: ProgressCallback | None, level: str, message: str) -> None:
    if callback:
        callback(ReaderEvent(level=level, message=message))


def encode_data(data: dict[str, object]) -> str:
    return "&".join(
        f"{key}={urllib.parse.quote(str(data[key]), safe='')}"
        for key in sorted(data.keys())
    )


def cal_hash(input_string: str) -> str:
    hash_a = 0x15051505
    hash_b = hash_a
    length = len(input_string)
    index = length - 1

    while index > 0:
        hash_a = 0x7FFFFFFF & (hash_a ^ ord(input_string[index]) << (length - index) % 30)
        hash_b = 0x7FFFFFFF & (hash_b ^ ord(input_string[index - 1]) << index % 30)
        index -= 2

    return hex(hash_a + hash_b)[2:].lower()


def get_wr_skey(config: ReaderConfig) -> str | None:
    response = requests.post(
        RENEW_URL,
        headers=config.headers,
        cookies=config.cookies,
        data=json.dumps(COOKIE_DATA, separators=(",", ":")),
        timeout=REQUEST_TIMEOUT,
    )
    for cookie in response.headers.get("Set-Cookie", "").split(";"):
        if "wr_skey" in cookie:
            return cookie.split("=")[-1][:8]
    return None


def fix_no_synckey(config: ReaderConfig) -> None:
    requests.post(
        FIX_SYNCKEY_URL,
        headers=config.headers,
        cookies=config.cookies,
        data=json.dumps({"bookIds": ["3300060341"]}, separators=(",", ":")),
        timeout=REQUEST_TIMEOUT,
    )


def refresh_cookie(config: ReaderConfig, callback: ProgressCallback | None = None) -> None:
    emit(callback, "INFO", "刷新 cookie")
    new_skey = get_wr_skey(config)
    if new_skey:
        config.cookies["wr_skey"] = new_skey
        emit(callback, "INFO", f"密钥刷新成功：{new_skey}")
        return
    raise RuntimeError("无法获取新密钥，WXREAD_CURL_BASH 可能已失效")


def test_login_state(config: ReaderConfig) -> LoginTestResult:
    try:
        new_skey = get_wr_skey(config)
        if not new_skey:
            return LoginTestResult(False, "无法获取 wr_skey，登录态可能已过期")
        config.cookies["wr_skey"] = new_skey
        fix_no_synckey(config)
        return LoginTestResult(True, f"登录态有效，已刷新 wr_skey：{new_skey}")
    except Exception as exc:
        return LoginTestResult(False, f"登录态测试失败：{exc}")


def run_reading(
    config: ReaderConfig,
    sleep_seconds: int = 30,
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> ReaderResult:
    if config.selected_book_infos:
        return run_browser_reading(
            config,
            sleep_seconds,
            progress_callback,
            should_cancel,
        )
    success_count = 0
    try:
        refresh_cookie(config, progress_callback)
        emit(progress_callback, "INFO", f"一共需要阅读 {config.read_num} 次")
        last_time = int(time.time()) - 30
        payload = copy.deepcopy(config.data)

        while success_count < config.read_num:
            if is_cancelled(should_cancel):
                emit(progress_callback, "INFO", "任务已停止")
                return ReaderResult(status="cancelled", success_count=success_count)
            payload.pop("s", None)
            payload["b"] = random.choice(config.books)
            payload["c"] = random.choice(config.chapters)
            this_time = int(time.time())
            payload["ct"] = this_time
            payload["rt"] = this_time - last_time
            payload["ts"] = int(this_time * 1000) + random.randint(0, 1000)
            payload["rn"] = random.randint(0, 1000)
            payload["sg"] = hashlib.sha256(
                f"{payload['ts']}{payload['rn']}{KEY}".encode()
            ).hexdigest()
            payload["s"] = cal_hash(encode_data(payload))

            emit(progress_callback, "INFO", f"尝试第 {success_count + 1} 次阅读")
            response = requests.post(
                READ_URL,
                headers=config.headers,
                cookies=config.cookies,
                data=json.dumps(payload, separators=(",", ":")),
                timeout=REQUEST_TIMEOUT,
            )
            res_data = response.json()

            if "succ" in res_data:
                if "synckey" in res_data:
                    last_time = this_time
                    success_count += 1
                    if sleep_with_cancel(sleep_seconds, should_cancel):
                        emit(progress_callback, "INFO", "任务已停止")
                        return ReaderResult(status="cancelled", success_count=success_count)
                    emit(
                        progress_callback,
                        "INFO",
                        f"阅读成功，阅读进度：{success_count * 0.5} 分钟",
                    )
                else:
                    emit(progress_callback, "WARNING", "无 synckey，尝试修复")
                    fix_no_synckey(config)
            else:
                emit(progress_callback, "WARNING", "cookie 已过期，尝试刷新")
                refresh_cookie(config, progress_callback)

        emit(progress_callback, "INFO", "阅读脚本已完成")
        maybe_push_completion(config, success_count)
        return ReaderResult(status="success", success_count=success_count)
    except Exception as exc:
        message = str(exc)
        emit(progress_callback, "ERROR", message)
        maybe_push_error(config, message)
        return ReaderResult(status="failed", success_count=success_count, error=message)


def run_browser_reading(
    config: ReaderConfig,
    sleep_seconds: int,
    progress_callback: ProgressCallback | None,
    should_cancel: CancelCheck | None,
    playwright_factory: Callable[[], object] | None = None,
) -> ReaderResult:
    success_count = 0
    browser = None
    try:
        if playwright_factory is None:
            from playwright.sync_api import sync_playwright

            playwright_factory = sync_playwright

        refresh_cookie(config, progress_callback)
        if is_cancelled(should_cancel):
            return ReaderResult(status="cancelled", success_count=0)

        user_agent = next(
            (
                value
                for key, value in config.headers.items()
                if key.lower() == "user-agent"
            ),
            DEFAULT_BROWSER_USER_AGENT,
        )
        with playwright_factory() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=user_agent,
            )
            context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "domain": ".weread.qq.com",
                        "path": "/",
                    }
                    for name, value in config.cookies.items()
                ]
            )
            page = context.new_page()
            current_book_id = ""
            current_reader_url = ""
            page_turn_key = "ArrowRight"

            while success_count < config.read_num:
                if is_cancelled(should_cancel):
                    emit(progress_callback, "INFO", "任务已停止")
                    return ReaderResult(
                        status="cancelled",
                        success_count=success_count,
                    )

                book = random.choice(config.selected_book_infos)
                if book.book_id != current_book_id:
                    current_reader_url = resolve_reader_url(page, book)
                    page.goto(
                        current_reader_url,
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                    page.wait_for_timeout(5_000)
                    current_book_id = book.book_id
                    emit(progress_callback, "INFO", f"开始阅读：{book.title}")

                if sleep_with_cancel(sleep_seconds, should_cancel):
                    emit(progress_callback, "INFO", "任务已停止")
                    return ReaderResult(
                        status="cancelled",
                        success_count=success_count,
                    )

                response_data, page_turn_key = (
                    turn_page_with_boundary_recovery(
                        page,
                        page_turn_key,
                        current_reader_url,
                        progress_callback,
                    )
                )
                if not (
                    response_data.get("succ")
                    and response_data.get("synckey") is not None
                ):
                    raise RuntimeError(
                        f"微信读书未确认阅读成功：{response_data!r}"
                    )

                success_count += 1
                emit(
                    progress_callback,
                    "INFO",
                    f"阅读成功：{success_count}/{config.read_num}",
                )

            browser.close()
            browser = None

        maybe_push_completion(config, success_count)
        return ReaderResult(status="success", success_count=success_count)
    except Exception as exc:
        message = str(exc)
        emit(progress_callback, "ERROR", message)
        maybe_push_error(config, message)
        return ReaderResult(
            status="failed",
            success_count=success_count,
            error=message,
        )


def resolve_reader_url(page: object, book: BookInfo) -> str:
    page.goto(
        WEREAD_HOME,
        wait_until="domcontentloaded",
        timeout=45_000,
    )
    page.wait_for_timeout(4_000)
    search = page.locator("input[type=search]").first
    search.fill(book.title)
    search.press("Enter")
    page.wait_for_timeout(4_000)

    title_nodes = page.get_by_text(book.title, exact=True)
    for index in range(title_nodes.count()):
        href = title_nodes.nth(index).evaluate(
            "element => {"
            " const link = element.closest('a');"
            " return link ? link.href : null;"
            "}"
        )
        if href and href.startswith(f"{WEREAD_HOME}web/reader/"):
            return href
    raise RuntimeError(f"无法打开所选书目：{book.title}")


def turn_page_with_boundary_recovery(
    page: object,
    direction: str,
    reader_url: str,
    progress_callback: ProgressCallback | None = None,
) -> tuple[dict[str, object], str]:
    try:
        return wait_for_page_turn(page, direction), direction
    except PlaywrightTimeoutError:
        emit(
            progress_callback,
            "WARNING",
            "已读到书末，自动从头开始",
        )
        return restart_from_first_chapter(page, reader_url), "ArrowRight"


def wait_for_page_turn(page: object, direction: str) -> dict[str, object]:
    with page.expect_response(
        lambda response: response.url == READ_URL,
        timeout=BROWSER_RESPONSE_TIMEOUT_MS,
    ) as response_info:
        page.keyboard.press(direction)
    return response_info.value.json()


def restart_from_first_chapter(
    page: object,
    reader_url: str,
) -> dict[str, object]:
    page.goto(
        reader_url,
        wait_until="domcontentloaded",
        timeout=45_000,
    )
    page.wait_for_timeout(5_000)
    page.locator(
        "button.readerControls_item.catalog, button.rbb_item.catalog"
    ).first.click()
    page.wait_for_timeout(500)
    first_chapter = page.locator(
        "li.readerCatalog_list_item:not(.readerCatalog_list_item_disabled)"
    ).first
    with page.expect_response(
        lambda response: response.url == READ_URL,
        timeout=BROWSER_RESPONSE_TIMEOUT_MS,
    ) as response_info:
        first_chapter.click()
    return response_info.value.json()


def is_cancelled(should_cancel: CancelCheck | None) -> bool:
    return bool(should_cancel and should_cancel())


def sleep_with_cancel(seconds: int, should_cancel: CancelCheck | None) -> bool:
    if seconds <= 0:
        return is_cancelled(should_cancel)
    remaining = float(seconds)
    while remaining > 0:
        if is_cancelled(should_cancel):
            return True
        interval = min(1.0, remaining)
        time.sleep(interval)
        remaining -= interval
    return is_cancelled(should_cancel)


def maybe_push_completion(config: ReaderConfig, success_count: int) -> None:
    if not config.push_method:
        return
    push(
        f"微信读书自动阅读完成！\n阅读时长：{success_count * 0.5}分钟。",
        config.push_method,
        PushSettings.from_reader_config(config),
    )


def maybe_push_error(config: ReaderConfig, message: str) -> None:
    if not config.push_method:
        return
    push(message, config.push_method, PushSettings.from_reader_config(config))
