from pathlib import Path

import pytest

import db


def test_init_db_creates_defaults(tmp_path: Path):
    db_path = tmp_path / "wxread.sqlite3"

    db.init_db(db_path)

    settings = db.get_settings(db_path)
    schedule = db.get_schedule(db_path)
    assert settings["READ_NUM"] == "40"
    assert settings["PUSH_METHOD"] == ""
    assert settings["BOOK_LIBRARY"] == ""
    assert schedule == {"enabled": False, "time_of_day": "01:00"}


def test_update_settings_ignores_unknown_keys(tmp_path: Path):
    db_path = tmp_path / "wxread.sqlite3"

    db.update_settings(
        {"READ_NUM": "8", "PUSH_METHOD": "serverchan", "UNKNOWN": "value"},
        db_path,
    )

    settings = db.get_settings(db_path)
    assert settings["READ_NUM"] == "8"
    assert settings["PUSH_METHOD"] == "serverchan"
    assert "UNKNOWN" not in settings


def test_masked_settings_hide_secret_values(tmp_path: Path):
    db_path = tmp_path / "wxread.sqlite3"
    db.update_settings(
        {
            "WXREAD_CURL_BASH": "curl 'https://weread.qq.com/web/book/read' -H 'Cookie: abc=def'",
            "PUSHPLUS_TOKEN": "abcd1234efgh5678",
            "READ_NUM": "12",
        },
        db_path,
    )

    masked = db.get_masked_settings(db_path)

    assert masked["READ_NUM"] == "12"
    assert masked["WXREAD_CURL_BASH"].startswith("已配置")
    assert "weread" not in masked["WXREAD_CURL_BASH"]
    assert masked["PUSHPLUS_TOKEN"] == "已配置 (abcd...5678)"


def test_validate_time_of_day_normalizes_and_rejects_bad_values():
    assert db.validate_time_of_day("1:05") == "01:05"
    assert db.validate_time_of_day("23:59") == "23:59"

    with pytest.raises(ValueError):
        db.validate_time_of_day("24:00")
    with pytest.raises(ValueError):
        db.validate_time_of_day("nope")


def test_schedule_roundtrip(tmp_path: Path):
    db_path = tmp_path / "wxread.sqlite3"

    db.update_schedule(True, "6:30", db_path)

    assert db.get_schedule(db_path) == {"enabled": True, "time_of_day": "06:30"}


def test_run_and_logs_roundtrip(tmp_path: Path):
    db_path = tmp_path / "wxread.sqlite3"

    run_id = db.create_run(3, db_path)
    db.add_run_log(run_id, "INFO", "开始运行", db_path)
    db.finish_run(run_id, "success", 3, db_path=db_path)

    runs = db.list_runs(db_path=db_path)
    logs = db.list_run_logs(run_id, db_path)
    assert runs[0]["id"] == run_id
    assert runs[0]["status"] == "success"
    assert runs[0]["success_count"] == 3
    assert logs[0]["message"] == "开始运行"
    assert db.has_active_run(db_path) is False


def test_delete_run_removes_finished_run_and_logs(tmp_path: Path):
    db_path = tmp_path / "wxread.sqlite3"
    run_id = db.create_run(3, db_path)
    db.add_run_log(run_id, "INFO", "开始运行", db_path)
    db.finish_run(run_id, "success", 3, db_path=db_path)

    deleted, message = db.delete_run(run_id, db_path)

    assert deleted is True
    assert message == "任务已删除"
    assert db.list_runs(db_path=db_path) == []
    assert db.list_run_logs(run_id, db_path) == []


def test_delete_run_rejects_running_run(tmp_path: Path):
    db_path = tmp_path / "wxread.sqlite3"
    run_id = db.create_run(3, db_path)

    deleted, message = db.delete_run(run_id, db_path)

    assert deleted is False
    assert message == "正在运行的任务不能删除"
    assert db.has_active_run(db_path) is True


def test_cancel_run_marks_running_run_cancelled(tmp_path: Path):
    db_path = tmp_path / "wxread.sqlite3"
    run_id = db.create_run(3, db_path)

    cancelled, message = db.cancel_run(run_id, success_count=1, db_path=db_path)

    runs = db.list_runs(db_path=db_path)
    assert cancelled is True
    assert message == "任务已停止"
    assert runs[0]["status"] == "cancelled"
    assert runs[0]["success_count"] == 1
    assert runs[0]["finished_at"]
    assert db.has_active_run(db_path) is False


def test_cancel_run_rejects_finished_run(tmp_path: Path):
    db_path = tmp_path / "wxread.sqlite3"
    run_id = db.create_run(3, db_path)
    db.finish_run(run_id, "success", 3, db_path=db_path)

    cancelled, message = db.cancel_run(run_id, db_path=db_path)

    assert cancelled is False
    assert message == "任务已经结束"
