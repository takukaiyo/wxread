from pathlib import Path

import db
import scheduler


def test_sync_schedule_adds_and_removes_job(tmp_path: Path):
    db_path = tmp_path / "wxread.sqlite3"
    db.update_schedule(True, "06:30", db_path)
    sched = scheduler.create_scheduler()

    try:
        scheduler.sync_schedule(sched, db_path, lambda source: None)
        job = sched.get_job(scheduler.WXREAD_JOB_ID)
        assert job is not None
        assert str(job.trigger).find("hour='6'") != -1

        db.update_schedule(False, "06:30", db_path)
        scheduler.sync_schedule(sched, db_path, lambda source: None)
        assert sched.get_job(scheduler.WXREAD_JOB_ID) is None
    finally:
        sched.shutdown(wait=False)
