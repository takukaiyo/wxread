"""SQLite persistence for the wxread control panel."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


DEFAULT_DB_PATH = Path("data/wxread.sqlite3")
SECRET_KEYS = {
    "WXREAD_CURL_BASH",
    "PUSHPLUS_TOKEN",
    "WXPUSHER_SPT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SERVERCHAN_SPT",
    "ADMIN_PASSWORD_HASH",
}
DEFAULT_SETTINGS = {
    "READ_NUM": "40",
    "PUSH_METHOD": "",
    "WXREAD_CURL_BASH": "",
    "SELECTED_BOOKS": "",
    "BOOK_LIBRARY": "",
    "PUSHPLUS_TOKEN": "",
    "WXPUSHER_SPT": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "SERVERCHAN_SPT": "",
    "ADMIN_PASSWORD_HASH": "",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL,
                time_of_day TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                read_num INTEGER NOT NULL,
                success_count INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS run_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            );
            """
        )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        conn.execute(
            "INSERT OR IGNORE INTO schedule (id, enabled, time_of_day) VALUES (1, 0, '01:00')"
        )


def get_settings(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, str]:
    init_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    values = DEFAULT_SETTINGS.copy()
    values.update({row["key"]: row["value"] for row in rows})
    return values


def update_settings(settings: dict[str, str], db_path: str | Path = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    with get_connection(db_path) as conn:
        for key, value in settings.items():
            if key not in DEFAULT_SETTINGS:
                continue
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value or ""),
            )


def mask_value(key: str, value: str) -> str:
    if key not in SECRET_KEYS:
        return value
    if not value:
        return "未配置"
    if len(value) <= 8:
        return "已配置"
    return f"已配置 ({value[:4]}...{value[-4:]})"


def get_masked_settings(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, str]:
    settings = get_settings(db_path)
    return {key: mask_value(key, value) for key, value in settings.items()}


def validate_time_of_day(time_of_day: str) -> str:
    parts = time_of_day.split(":")
    if len(parts) != 2:
        raise ValueError("时间必须使用 HH:MM 格式")
    hour, minute = parts
    if not (hour.isdigit() and minute.isdigit()):
        raise ValueError("时间必须使用数字")
    hour_i = int(hour)
    minute_i = int(minute)
    if hour_i < 0 or hour_i > 23 or minute_i < 0 or minute_i > 59:
        raise ValueError("时间超出范围")
    return f"{hour_i:02d}:{minute_i:02d}"


def get_schedule(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, object]:
    init_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT enabled, time_of_day FROM schedule WHERE id = 1"
        ).fetchone()
    return {"enabled": bool(row["enabled"]), "time_of_day": row["time_of_day"]}


def update_schedule(
    enabled: bool, time_of_day: str, db_path: str | Path = DEFAULT_DB_PATH
) -> None:
    valid_time = validate_time_of_day(time_of_day)
    init_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO schedule (id, enabled, time_of_day) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled, "
            "time_of_day = excluded.time_of_day",
            (1 if enabled else 0, valid_time),
        )


def create_run(read_num: int, db_path: str | Path = DEFAULT_DB_PATH) -> int:
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO runs (status, started_at, read_num) VALUES ('running', ?, ?)",
            (utc_now(), read_num),
        )
        return int(cursor.lastrowid)


def finish_run(
    run_id: int,
    status: str,
    success_count: int,
    error: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    if status not in {"success", "failed", "skipped", "cancelled"}:
        raise ValueError("无效的运行状态")
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, success_count = ?, error = ? "
            "WHERE id = ?",
            (status, utc_now(), success_count, error, run_id),
        )


def add_run_log(
    run_id: int,
    level: str,
    message: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO run_logs (run_id, created_at, level, message) VALUES (?, ?, ?, ?)",
            (run_id, utc_now(), level, message),
        )


def list_runs(limit: int = 20, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, object]]:
    init_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_run_logs(run_id: int, db_path: str | Path = DEFAULT_DB_PATH) -> list[dict[str, object]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM run_logs WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_run(run_id: int, db_path: str | Path = DEFAULT_DB_PATH) -> tuple[bool, str]:
    init_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return False, "任务不存在"
        if row["status"] == "running":
            return False, "正在运行的任务不能删除"
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    return True, "任务已删除"


def cancel_run(
    run_id: int,
    success_count: int = 0,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[bool, str]:
    init_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return False, "任务不存在"
        if row["status"] != "running":
            return False, "任务已经结束"
        conn.execute(
            "UPDATE runs SET status = 'cancelled', finished_at = ?, success_count = ? "
            "WHERE id = ?",
            (utc_now(), success_count, run_id),
        )
    return True, "任务已停止"


def has_active_run(db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    init_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE status = 'running' LIMIT 1"
        ).fetchone()
    return row is not None
