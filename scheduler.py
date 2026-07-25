"""APScheduler integration for wxread scheduled runs."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import db


WXREAD_JOB_ID = "wxread_daily"


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.start(paused=False)
    return scheduler


def sync_schedule(
    scheduler: BackgroundScheduler,
    db_path: str | Path,
    run_callback: Callable[[str], None],
) -> None:
    schedule = db.get_schedule(db_path)
    existing = scheduler.get_job(WXREAD_JOB_ID)
    if not schedule["enabled"]:
        if existing:
            scheduler.remove_job(WXREAD_JOB_ID)
        return

    hour, minute = str(schedule["time_of_day"]).split(":")
    trigger = CronTrigger(hour=int(hour), minute=int(minute), timezone="Asia/Shanghai")
    scheduler.add_job(
        lambda: run_callback("schedule"),
        trigger=trigger,
        id=WXREAD_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
