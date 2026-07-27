import json

import pytest

import reader


class FakeResponse:
    def __init__(self, payload=None, headers=None):
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def test_parse_curl_command_extracts_headers_and_cookies():
    curl = (
        "curl 'https://weread.qq.com/web/book/read' "
        "-H 'accept: application/json' "
        "-H 'user-agent: Test Browser' "
        "-H 'Cookie: wr_skey=oldkey; pac_uid=abc' "
        "--data-raw '{\"b\":\"book\"}'"
    )

    parsed = reader.parse_curl_command(curl)

    assert parsed.headers["accept"] == "application/json"
    assert parsed.headers["user-agent"] == "Test Browser"
    assert "Cookie" not in parsed.headers
    assert parsed.cookies == {"wr_skey": "oldkey", "pac_uid": "abc"}


def test_parse_curl_command_rejects_empty_cookie():
    with pytest.raises(ValueError, match="Cookie"):
        reader.parse_curl_command("curl 'https://weread.qq.com/web/book/read'")


def test_build_config_from_settings_uses_curl_values():
    settings = {
        "READ_NUM": "2",
        "PUSH_METHOD": "serverchan",
        "SERVERCHAN_SPT": "token",
        "WXREAD_CURL_BASH": (
            "curl 'https://weread.qq.com/web/book/read' "
            "-H 'accept: application/json' "
            "-b 'wr_skey=oldkey; pac_uid=abc'"
        ),
        "SELECTED_BOOKS": "book1,book2 book1",
        "BOOK_LIBRARY": json.dumps(
            [
                {"bookId": "book1", "title": "Book One"},
                {"bookId": "book2", "title": "Book Two"},
                {"bookId": "book3", "title": "Book Three"},
            ]
        ),
    }

    config = reader.build_config_from_settings(settings)

    assert config.read_num == 2
    assert config.push_method == "serverchan"
    assert config.serverchan_spt == "token"
    assert config.headers["accept"] == "application/json"
    assert config.cookies["wr_skey"] == "oldkey"
    assert config.books == ["book1", "book2"]
    assert config.selected_book_infos == [
        reader.BookInfo("book1", "Book One"),
        reader.BookInfo("book2", "Book Two"),
    ]


def test_book_library_roundtrip():
    books = [
        reader.BookInfo("695233", "三体全集（全三册）", "刘慈欣", "https://example.com/cover.jpg"),
        reader.BookInfo("bad id", "bad"),
    ]

    value = reader.serialize_book_library(books)
    parsed = reader.parse_book_library(value)

    assert len(parsed) == 1
    assert parsed[0].book_id == "695233"
    assert parsed[0].title == "三体全集（全三册）"
    assert parsed[0].author == "刘慈欣"


def test_search_weread_books_parses_results(monkeypatch):
    class FakeSearchResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "books": [
                    {
                        "bookInfo": {
                            "bookId": "695233",
                            "title": "三体全集（全三册）",
                            "author": "刘慈欣",
                            "cover": "https://example.com/cover.jpg",
                        }
                    },
                    {"bookInfo": {"bookId": "bad id", "title": "坏数据"}},
                ]
            }

    def fake_get(url, params, timeout):
        assert url == reader.SEARCH_URL
        assert params == {"keyword": "三体"}
        assert timeout == 10
        return FakeSearchResponse()

    monkeypatch.setattr(reader.requests, "get", fake_get)

    results = reader.search_weread_books("三体")

    assert results == [
        reader.BookInfo("695233", "三体全集（全三册）", "刘慈欣", "https://example.com/cover.jpg")
    ]


def test_test_login_state_success(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        if url == reader.RENEW_URL:
            return FakeResponse(headers={"Set-Cookie": "wr_skey=newkey; Path=/;"})
        return FakeResponse({"succ": 1})

    monkeypatch.setattr(reader.requests, "post", fake_post)
    config = reader.ReaderConfig(
        read_num=1,
        headers={"accept": "application/json"},
        cookies={"wr_skey": "oldkey"},
    )

    result = reader.test_login_state(config)

    assert result.ok is True
    assert "newkey" in result.message
    assert calls == [reader.RENEW_URL, reader.FIX_SYNCKEY_URL]


def test_test_login_state_failure(monkeypatch):
    def fake_post(url, **kwargs):
        return FakeResponse(headers={})

    monkeypatch.setattr(reader.requests, "post", fake_post)
    config = reader.ReaderConfig(read_num=1, headers={}, cookies={})

    result = reader.test_login_state(config)

    assert result.ok is False
    assert "无法获取" in result.message


def test_run_reading_success(monkeypatch):
    posts = []
    events = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs.get("data")))
        if url == reader.RENEW_URL:
            return FakeResponse(headers={"Set-Cookie": "wr_skey=newkey; Path=/;"})
        return FakeResponse({"succ": 1, "synckey": 123})

    monkeypatch.setattr(reader.requests, "post", fake_post)
    config = reader.ReaderConfig(
        read_num=2,
        headers={"accept": "application/json"},
        cookies={"wr_skey": "oldkey"},
        data=reader.DEFAULT_DATA.copy(),
        books=["book-a"],
        chapters=["chapter-a"],
    )

    result = reader.run_reading(config, sleep_seconds=0, progress_callback=events.append)

    assert result.status == "success"
    assert result.success_count == 2
    assert any(event.message == "阅读成功，阅读进度：1.0 分钟" for event in events)
    read_payloads = [
        json.loads(payload)
        for url, payload in posts
        if url == reader.READ_URL
    ]
    assert len(read_payloads) == 2
    assert all(payload["b"] == "book-a" for payload in read_payloads)


def test_run_reading_can_be_cancelled_before_next_read(monkeypatch):
    posts = []
    events = []

    def fake_post(url, **kwargs):
        posts.append(url)
        if url == reader.RENEW_URL:
            return FakeResponse(headers={"Set-Cookie": "wr_skey=newkey; Path=/;"})
        return FakeResponse({"succ": 1, "synckey": 123})

    monkeypatch.setattr(reader.requests, "post", fake_post)
    config = reader.ReaderConfig(
        read_num=2,
        headers={"accept": "application/json"},
        cookies={"wr_skey": "oldkey"},
        data=reader.DEFAULT_DATA.copy(),
        books=["book-a"],
        chapters=["chapter-a"],
    )

    result = reader.run_reading(
        config,
        sleep_seconds=0,
        progress_callback=events.append,
        should_cancel=lambda: True,
    )

    assert result.status == "cancelled"
    assert result.success_count == 0
    assert posts == [reader.RENEW_URL]
    assert any(event.message == "任务已停止" for event in events)


def test_run_reading_uses_browser_for_bookstore_selection(monkeypatch):
    expected = reader.ReaderResult(status="success", success_count=1)
    calls = []

    def fake_browser_run(config, sleep_seconds, progress_callback, should_cancel):
        calls.append((config, sleep_seconds, progress_callback, should_cancel))
        return expected

    monkeypatch.setattr(reader, "run_browser_reading", fake_browser_run)
    callback = lambda event: None
    cancel = lambda: False
    config = reader.ReaderConfig(
        read_num=1,
        selected_book_infos=[reader.BookInfo("3300108590", "Book One")],
    )

    result = reader.run_reading(
        config,
        sleep_seconds=30,
        progress_callback=callback,
        should_cancel=cancel,
    )

    assert result is expected
    assert calls == [(config, 30, callback, cancel)]


def test_run_browser_reading_counts_official_success(monkeypatch):
    events = []

    class FakeApiResponse:
        url = reader.READ_URL

        def json(self):
            return {"succ": 1, "synckey": 123}

    class FakeResponseInfo:
        value = FakeApiResponse()

    class FakeExpectedResponse:
        def __enter__(self):
            return FakeResponseInfo()

        def __exit__(self, exc_type, exc, traceback):
            return False

    class FakeKeyboard:
        def __init__(self):
            self.keys = []

        def press(self, key):
            self.keys.append(key)

    class FakePage:
        def __init__(self):
            self.keyboard = FakeKeyboard()
            self.urls = []

        def goto(self, url, **kwargs):
            self.urls.append(url)

        def wait_for_timeout(self, milliseconds):
            return None

        def expect_response(self, predicate, timeout):
            assert timeout == 20_000
            assert predicate(FakeApiResponse())
            return FakeExpectedResponse()

    class FakeContext:
        def __init__(self):
            self.page = FakePage()
            self.cookies = []

        def add_cookies(self, cookies):
            self.cookies = cookies

        def new_page(self):
            return self.page

    class FakeBrowser:
        def __init__(self):
            self.context = FakeContext()
            self.closed = False
            self.manager = None

        def new_context(self, **kwargs):
            return self.context

        def close(self):
            assert self.manager is not None
            assert self.manager.exited is False
            self.closed = True

    class FakeChromium:
        def __init__(self):
            self.browser = FakeBrowser()

        def launch(self, **kwargs):
            return self.browser

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

    class FakePlaywrightManager:
        def __init__(self):
            self.playwright = FakePlaywright()
            self.exited = False
            self.playwright.chromium.browser.manager = self

        def __enter__(self):
            return self.playwright

        def __exit__(self, exc_type, exc, traceback):
            self.exited = True
            return False

    manager = FakePlaywrightManager()
    monkeypatch.setattr(reader, "refresh_cookie", lambda config, callback=None: None)
    monkeypatch.setattr(
        reader,
        "resolve_reader_url",
        lambda page, book: f"https://weread.qq.com/web/reader/{book.book_id}",
    )
    config = reader.ReaderConfig(
        read_num=1,
        cookies={"wr_skey": "key", "wr_vid": "123"},
        selected_book_infos=[reader.BookInfo("3300108590", "Book One")],
    )

    result = reader.run_browser_reading(
        config,
        sleep_seconds=0,
        progress_callback=events.append,
        should_cancel=None,
        playwright_factory=lambda: manager,
    )

    browser = manager.playwright.chromium.browser
    assert result == reader.ReaderResult(status="success", success_count=1)
    assert browser.context.cookies == [
        {
            "name": "wr_skey",
            "value": "key",
            "domain": ".weread.qq.com",
            "path": "/",
        },
        {
            "name": "wr_vid",
            "value": "123",
            "domain": ".weread.qq.com",
            "path": "/",
        },
    ]
    assert browser.context.page.urls == [
        "https://weread.qq.com/web/reader/3300108590"
    ]
    assert browser.context.page.keyboard.keys == ["ArrowRight"]
    assert browser.closed is True
    assert any("1/1" in event.message for event in events)
