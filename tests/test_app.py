from pathlib import Path
import threading

import pytest

import app as wxapp
import db
import qr_login
from reader import LoginTestResult, ReaderResult


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "wxread.sqlite3"
    monkeypatch.setenv("WXREAD_ADMIN_PASSWORD", "secret")
    application = wxapp.create_app(db_path=db_path, start_scheduler=False)
    application.config.update(TESTING=True)
    with application.test_client() as client:
        yield client, db_path


def login(client):
    return client.post("/login", data={"password": "secret"}, follow_redirects=True)


def test_dashboard_requires_login(client):
    client, _ = client

    response = client.get("/")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_success_renders_dashboard(client):
    client, _ = client

    response = login(client)

    assert response.status_code == 200
    assert "微信读书控制面板".encode() in response.data


def test_save_settings_keeps_blank_secret_values(client):
    client, db_path = client
    db.update_settings({"PUSHPLUS_TOKEN": "old-secret"}, db_path)
    login(client)

    response = client.post(
        "/settings",
        data={
            "READ_NUM": "12",
            "PUSH_METHOD": "pushplus",
            "PUSHPLUS_TOKEN": "",
            "SELECTED_BOOKS": [wxapp.reader.DEFAULT_BOOKS[0]],
            "BOOK_ID": [wxapp.reader.DEFAULT_BOOKS[0]],
            "BOOK_TITLE": ["测试书"],
            "BOOK_AUTHOR": ["测试作者"],
            "BOOK_COVER": [""],
        },
        follow_redirects=True,
    )

    settings = db.get_settings(db_path)
    assert response.status_code == 200
    assert settings["READ_NUM"] == "12"
    assert settings["PUSH_METHOD"] == "pushplus"
    assert settings["PUSHPLUS_TOKEN"] == "old-secret"
    assert settings["SELECTED_BOOKS"] == wxapp.reader.DEFAULT_BOOKS[0]
    assert "测试书" in settings["BOOK_LIBRARY"]
    assert b"old-secret" not in response.data


def test_create_app_clears_legacy_default_books(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "wxread.sqlite3"
    monkeypatch.setenv("WXREAD_ADMIN_PASSWORD", "secret")
    db.update_settings({"SELECTED_BOOKS": ",".join(wxapp.reader.DEFAULT_BOOKS)}, db_path)

    wxapp.create_app(db_path=db_path, start_scheduler=False)

    assert db.get_settings(db_path)["SELECTED_BOOKS"] == ""


def test_book_search_endpoint(client, monkeypatch):
    client, _ = client
    login(client)
    monkeypatch.setattr(
        wxapp.reader,
        "search_weread_books",
        lambda keyword: [wxapp.reader.BookInfo("695233", "三体全集（全三册）", "刘慈欣", "cover")],
    )

    response = client.get("/api/books/search?q=三体")

    assert response.status_code == 200
    assert response.json["books"] == [
        {
            "bookId": "695233",
            "title": "三体全集（全三册）",
            "author": "刘慈欣",
            "cover": "cover",
        }
    ]


def test_curl_login_route_saves_curl(client):
    client, db_path = client
    login(client)

    response = client.post(
        "/curl-login",
        data={"WXREAD_CURL_BASH": "curl 'x' -b 'wr_skey=abc'"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert db.get_settings(db_path)["WXREAD_CURL_BASH"] == "curl 'x' -b 'wr_skey=abc'"


def test_test_login_endpoint(client, monkeypatch):
    client, _ = client
    login(client)
    monkeypatch.setattr(
        wxapp.reader,
        "test_login_state",
        lambda config: LoginTestResult(True, "登录态有效"),
    )

    response = client.post("/api/test-login")

    assert response.json == {"ok": True, "message": "登录态有效"}


def test_manual_run_starts_background_job(client, monkeypatch):
    client, db_path = client
    login(client)
    db.update_settings({"SELECTED_BOOKS": "book-a"}, db_path)

    def fake_run(config, progress_callback=None, should_cancel=None):
        progress_callback(wxapp.reader.ReaderEvent("INFO", "done"))
        return ReaderResult("success", 1)

    monkeypatch.setattr(wxapp.reader, "run_reading", fake_run)

    response = client.post("/run", follow_redirects=True)

    assert response.status_code == 200
    runs = db.list_runs(db_path=db_path)
    assert runs[0]["status"] == "success"
    messages = [row["message"] for row in db.list_run_logs(runs[0]["id"], db_path)]
    assert "done" in messages


def test_manual_run_requires_selected_books(client, monkeypatch):
    client, _ = client
    login(client)

    response = client.post("/run", follow_redirects=True)

    assert response.status_code == 200
    assert "请先在书城搜索并选择至少一本书".encode() in response.data


def test_delete_run_route_removes_finished_run(client):
    client, db_path = client
    login(client)
    run_id = db.create_run(2, db_path)
    db.finish_run(run_id, "success", 2, db_path=db_path)

    response = client.post(f"/runs/{run_id}/delete", follow_redirects=True)

    assert response.status_code == 200
    assert db.list_runs(db_path=db_path) == []
    assert "任务已删除".encode() in response.data


def test_delete_run_route_rejects_running_run(client):
    client, db_path = client
    login(client)
    run_id = db.create_run(2, db_path)

    response = client.post(f"/runs/{run_id}/delete", follow_redirects=True)

    assert response.status_code == 200
    assert db.has_active_run(db_path) is True
    assert "正在运行的任务不能删除".encode() in response.data


def test_stop_run_route_sets_active_cancel_event(client):
    client, db_path = client
    login(client)
    run_id = db.create_run(2, db_path)
    cancel_event = threading.Event()
    client.application.config["ACTIVE_RUNS"][run_id] = cancel_event

    response = client.post(f"/runs/{run_id}/stop", follow_redirects=True)

    assert response.status_code == 200
    assert cancel_event.is_set() is True
    assert "已发送停止信号".encode() in response.data


def test_stop_run_route_cancels_stale_running_run(client):
    client, db_path = client
    login(client)
    run_id = db.create_run(2, db_path)

    response = client.post(f"/runs/{run_id}/stop", follow_redirects=True)

    assert response.status_code == 200
    assert db.list_runs(db_path=db_path)[0]["status"] == "cancelled"
    assert "任务已停止".encode() in response.data


def test_schedule_save_validates_time(client):
    client, db_path = client
    login(client)

    response = client.post(
        "/schedule",
        data={"enabled": "on", "time_of_day": "6:05"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert db.get_schedule(db_path) == {"enabled": True, "time_of_day": "06:05"}


class FakeQrManager:
    def __init__(self):
        self.state = qr_login.QrLoginState(
            session_id="abc",
            status="success",
            message="QR login completed",
            curl_bash="curl 'x' -b 'wr_skey=abc'",
        )

    def start(self):
        return self.state

    def status(self):
        return self.state

    def mark_saved(self):
        self.state.saved = True


def test_qr_login_status_saves_curl(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "wxread.sqlite3"
    monkeypatch.setenv("WXREAD_ADMIN_PASSWORD", "secret")
    application = wxapp.create_app(
        db_path=db_path,
        start_scheduler=False,
        qr_manager=FakeQrManager(),
    )
    application.config.update(TESTING=True)

    with application.test_client() as client:
        login(client)
        started = client.post("/api/qr-login/start")
        status = client.get("/api/qr-login/status")

    assert started.json["status"] == "success"
    assert status.json["saved"] is True
    assert db.get_settings(db_path)["WXREAD_CURL_BASH"] == "curl 'x' -b 'wr_skey=abc'"
