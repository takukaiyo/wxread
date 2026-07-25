"""Flask control panel for wxread."""

from __future__ import annotations

import argparse
import os
import threading
from functools import wraps
from pathlib import Path
from typing import Callable

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import db
import qr_login
import reader
import scheduler as scheduler_module


SECRET_FORM_KEYS = {
    "WXREAD_CURL_BASH",
    "PUSHPLUS_TOKEN",
    "WXPUSHER_SPT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SERVERCHAN_SPT",
}
SETTING_FORM_KEYS = {
    "READ_NUM",
    "PUSH_METHOD",
    "PUSHPLUS_TOKEN",
    "WXPUSHER_SPT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SERVERCHAN_SPT",
}


def create_app(
    db_path: str | Path = db.DEFAULT_DB_PATH,
    start_scheduler: bool = True,
    qr_manager: qr_login.QrLoginManager | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("WXREAD_PANEL_SECRET", os.urandom(24).hex())
    app.config["DB_PATH"] = Path(db_path)
    app.config["RUN_LOCK"] = threading.Lock()
    app.config["ACTIVE_RUNS"] = {}
    app.config["SCHEDULER"] = None
    app.config["QR_LOGIN"] = qr_manager or qr_login.QrLoginManager(Path(db_path).parent)
    db.init_db(app.config["DB_PATH"])
    ensure_admin_password(app.config["DB_PATH"])
    clear_legacy_default_books(app.config["DB_PATH"])

    if start_scheduler:
        sched = scheduler_module.create_scheduler()
        app.config["SCHEDULER"] = sched
        scheduler_module.sync_schedule(sched, app.config["DB_PATH"], lambda source: start_run(app, source))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            password = request.form.get("password", "")
            settings = db.get_settings(app.config["DB_PATH"])
            if check_password_hash(settings["ADMIN_PASSWORD_HASH"], password):
                session["authenticated"] = True
                return redirect(url_for("dashboard"))
            flash("密码不正确", "error")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        db_path = app.config["DB_PATH"]
        return render_template(
            "dashboard.html",
            settings=db.get_settings(db_path),
            masked_settings=db.get_masked_settings(db_path),
            schedule=db.get_schedule(db_path),
            runs=db.list_runs(db_path=db_path),
            active_run=db.has_active_run(db_path),
            push_methods=["", "pushplus", "wxpusher", "telegram", "serverchan"],
            book_options=build_book_options(db.get_settings(db_path)),
        )

    @app.route("/settings", methods=["POST"])
    @login_required
    def save_settings():
        db_path = app.config["DB_PATH"]
        current = db.get_settings(db_path)
        updates: dict[str, str] = {}
        for key in SETTING_FORM_KEYS:
            value = request.form.get(key, "")
            if key in SECRET_FORM_KEYS and value == "":
                continue
            updates[key] = value
        selected_books = [
            book_id
            for book_id in request.form.getlist("SELECTED_BOOKS")
            if reader.is_valid_book_id(book_id)
        ]
        if not selected_books:
            flash("请先在书城搜索并选择至少一本书", "error")
            return redirect(url_for("dashboard"))
        updates["SELECTED_BOOKS"] = ",".join(selected_books)
        updates["BOOK_LIBRARY"] = reader.serialize_book_library(
            build_book_library_from_form(selected_books)
        )
        password = request.form.get("ADMIN_PASSWORD", "")
        if password:
            updates["ADMIN_PASSWORD_HASH"] = generate_password_hash(password)
        try:
            read_num = int(updates.get("READ_NUM", current.get("READ_NUM", "40")))
            if read_num < 1 or read_num > 500:
                raise ValueError
        except ValueError:
            flash("READ_NUM 必须是 1 到 500 的整数", "error")
            return redirect(url_for("dashboard"))
        db.update_settings(updates, db_path)
        flash("配置已保存", "success")
        return redirect(url_for("dashboard"))

    @app.route("/curl-login", methods=["POST"])
    @login_required
    def save_curl_login():
        curl_bash = request.form.get("WXREAD_CURL_BASH", "")
        try:
            reader.parse_curl_command(curl_bash)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        db.update_settings({"WXREAD_CURL_BASH": curl_bash}, app.config["DB_PATH"])
        flash("curl 登录配置已保存", "success")
        return redirect(url_for("dashboard"))

    @app.route("/schedule", methods=["POST"])
    @login_required
    def save_schedule():
        enabled = request.form.get("enabled") == "on"
        time_of_day = request.form.get("time_of_day", "01:00")
        try:
            db.update_schedule(enabled, time_of_day, app.config["DB_PATH"])
            sched = app.config.get("SCHEDULER")
            if sched:
                scheduler_module.sync_schedule(
                    sched, app.config["DB_PATH"], lambda source: start_run(app, source)
                )
            flash("定时设置已保存", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.route("/api/test-login", methods=["POST"])
    @login_required
    def api_test_login():
        try:
            config = reader.build_config_from_settings(db.get_settings(app.config["DB_PATH"]))
            result = reader.test_login_state(config)
            return jsonify({"ok": result.ok, "message": result.message})
        except Exception as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

    @app.route("/api/books/search")
    @login_required
    def api_books_search():
        keyword = request.args.get("q", "")
        try:
            books = reader.search_weread_books(keyword)
        except Exception as exc:
            return jsonify({"ok": False, "message": f"书城搜索失败：{exc}", "books": []}), 400
        return jsonify(
            {
                "ok": True,
                "books": [
                    {
                        "bookId": book.book_id,
                        "title": book.title,
                        "author": book.author,
                        "cover": book.cover,
                    }
                    for book in books
                ],
            }
        )

    @app.route("/api/qr-login/start", methods=["POST"])
    @login_required
    def api_qr_login_start():
        state = app.config["QR_LOGIN"].start()
        return jsonify(qr_state_payload(state))

    @app.route("/api/qr-login/status")
    @login_required
    def api_qr_login_status():
        state = app.config["QR_LOGIN"].status()
        if state.status == "success" and state.curl_bash and not state.saved:
            db.update_settings({"WXREAD_CURL_BASH": state.curl_bash}, app.config["DB_PATH"])
            app.config["QR_LOGIN"].mark_saved()
            state.saved = True
        return jsonify(qr_state_payload(state))

    @app.route("/api/qr-login/image")
    @login_required
    def api_qr_login_image():
        state = app.config["QR_LOGIN"].status()
        if not state.image_path or not state.image_path.exists():
            return jsonify({"ok": False, "message": "QR image is not ready"}), 404
        return send_file(state.image_path, mimetype="image/png")

    @app.route("/run", methods=["POST"])
    @login_required
    def run_now():
        started, message = start_run(app, "manual")
        flash(message, "success" if started else "error")
        return redirect(url_for("dashboard"))

    @app.route("/runs/<int:run_id>/delete", methods=["POST"])
    @login_required
    def delete_run(run_id: int):
        deleted, message = db.delete_run(run_id, app.config["DB_PATH"])
        flash(message, "success" if deleted else "error")
        return redirect(url_for("dashboard"))

    @app.route("/runs/<int:run_id>/stop", methods=["POST"])
    @login_required
    def stop_run(run_id: int):
        active_runs: dict[int, threading.Event] = app.config["ACTIVE_RUNS"]
        cancel_event = active_runs.get(run_id)
        if cancel_event:
            cancel_event.set()
            flash("已发送停止信号", "success")
            return redirect(url_for("dashboard"))

        stopped, message = db.cancel_run(run_id, db_path=app.config["DB_PATH"])
        flash(message, "success" if stopped else "error")
        return redirect(url_for("dashboard"))

    @app.route("/api/runs")
    @login_required
    def api_runs():
        return jsonify(db.list_runs(db_path=app.config["DB_PATH"]))

    @app.route("/api/runs/<int:run_id>/logs")
    @login_required
    def api_run_logs(run_id: int):
        return jsonify(db.list_run_logs(run_id, app.config["DB_PATH"]))

    return app


def qr_state_payload(state: qr_login.QrLoginState) -> dict[str, object]:
    return {
        "session_id": state.session_id,
        "status": state.status,
        "message": state.message,
        "image_ready": bool(state.image_path and state.image_path.exists()),
        "saved": state.saved,
    }


def build_book_options(settings: dict[str, str]) -> list[dict[str, object]]:
    selected_ids = get_panel_selected_books(settings)
    library = {
        book.book_id: book
        for book in reader.parse_book_library(settings.get("BOOK_LIBRARY", ""))
    }
    options: list[dict[str, object]] = []
    for book_id in selected_ids:
        book = library.get(book_id)
        options.append(
            {
                "id": book_id,
                "title": book.title if book else "已选书目",
                "author": book.author if book else "",
                "cover": book.cover if book else "",
            }
        )
    return options


def build_book_library_from_form(selected_books: list[str]) -> list[reader.BookInfo]:
    titles = request.form.getlist("BOOK_TITLE")
    authors = request.form.getlist("BOOK_AUTHOR")
    covers = request.form.getlist("BOOK_COVER")
    indexed: dict[str, reader.BookInfo] = {}
    for index, book_id in enumerate(request.form.getlist("BOOK_ID")):
        if not reader.is_valid_book_id(book_id):
            continue
        indexed[book_id] = reader.BookInfo(
            book_id=book_id,
            title=(
                titles[index].strip()
                if index < len(titles) and titles[index].strip()
                else book_id
            ),
            author=authors[index].strip() if index < len(authors) else "",
            cover=covers[index].strip() if index < len(covers) else "",
        )
    return [
        indexed.get(book_id) or reader.BookInfo(book_id=book_id, title=book_id)
        for book_id in selected_books
    ]


def get_panel_selected_books(settings: dict[str, str]) -> list[str]:
    selected_ids = reader.parse_selected_books(settings.get("SELECTED_BOOKS", ""))
    if (
        selected_ids
        and not settings.get("BOOK_LIBRARY", "")
        and set(selected_ids) == set(reader.DEFAULT_BOOKS)
    ):
        return []
    return selected_ids


def login_required(view: Callable):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def ensure_admin_password(db_path: str | Path) -> None:
    settings = db.get_settings(db_path)
    if settings["ADMIN_PASSWORD_HASH"]:
        return
    initial_password = os.getenv("WXREAD_ADMIN_PASSWORD", "wxread")
    db.update_settings({"ADMIN_PASSWORD_HASH": generate_password_hash(initial_password)}, db_path)


def clear_legacy_default_books(db_path: str | Path) -> None:
    settings = db.get_settings(db_path)
    if settings.get("SELECTED_BOOKS") and not get_panel_selected_books(settings):
        db.update_settings({"SELECTED_BOOKS": ""}, db_path)


def start_run(app: Flask, source: str) -> tuple[bool, str]:
    db_path = app.config["DB_PATH"]
    lock: threading.Lock = app.config["RUN_LOCK"]
    if not lock.acquire(blocking=False):
        return False, "已有任务正在运行"
    if db.has_active_run(db_path):
        lock.release()
        return False, "已有任务正在运行"

    settings = db.get_settings(db_path)
    if not get_panel_selected_books(settings):
        lock.release()
        return False, "请先在书城搜索并选择至少一本书"
    try:
        config = reader.build_config_from_settings(settings)
    except Exception as exc:
        lock.release()
        return False, f"配置无效：{exc}"

    run_id = db.create_run(config.read_num, db_path)
    cancel_event = threading.Event()
    app.config["ACTIVE_RUNS"][run_id] = cancel_event
    db.add_run_log(run_id, "INFO", f"{source} 任务已启动", db_path)

    def worker() -> None:
        try:
            def on_event(event: reader.ReaderEvent) -> None:
                db.add_run_log(run_id, event.level, event.message, db_path)

            result = reader.run_reading(
                config,
                progress_callback=on_event,
                should_cancel=cancel_event.is_set,
            )
            db.finish_run(
                run_id,
                result.status,
                result.success_count,
                result.error,
                db_path,
            )
        finally:
            app.config["ACTIVE_RUNS"].pop(run_id, None)
            lock.release()

    if app.config.get("TESTING"):
        worker()
    else:
        threading.Thread(target=worker, name=f"wxread-run-{run_id}", daemon=True).start()
    return True, f"任务已启动，运行 ID：{run_id}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="wxread control panel")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    create_app(args.db).run(host=args.host, port=args.port)
