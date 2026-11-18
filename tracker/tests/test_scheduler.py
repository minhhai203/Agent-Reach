"""Scheduler: pure schedule functions and tick() with frozen time and fake jobs."""

from __future__ import annotations

import datetime as dt
import logging
import threading

import pytest

from conftest import vn
from video_tracker import config, core, db, scheduler

# 2026-11-16 is a Monday; the previous week starts 2026-11-09.
MON = (2026, 11, 16)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("mon 08:00", (0, 8, 0)),
        ("MON 08:00", (0, 8, 0)),
        ("  Tue 7:05 ", (1, 7, 5)),
        ("sun 23:59", (6, 23, 59)),
        ("fri 17:30", (4, 17, 30)),
        ("t2 08:00", (0, 8, 0)),
        ("T7 12:00", (5, 12, 0)),
        ("cn 00:00", (6, 0, 0)),
        ("CN 06:15", (6, 6, 15)),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_parse_report_at(text, expected):
    assert scheduler.parse_report_at(text) == expected


@pytest.mark.parametrize(
    "text",
    ["monday 08:00", "mon 24:00", "mon 08:60", "mon", "08:00", "t8 08:00", "t1 08:00",
     "mon 8", "x 08:00", "mon08:00", "mon 08:00:00", "mon -1:00"],
)
def test_parse_report_at_invalid(text):
    with pytest.raises(ValueError, match="TRACKER_REPORT_AT"):
        scheduler.parse_report_at(text)


@pytest.mark.parametrize(
    "now,report_at,last,expected",
    [
        (vn(*MON, 7, 59), "mon 08:00", None, False),            # before the scheduled time
        (vn(*MON, 8, 0), "mon 08:00", None, True),              # exactly on time
        (vn(*MON, 8, 0), "mon 08:00", "2026-11-09", False),     # already delivered
        (vn(*MON, 8, 0), "mon 08:00", "2026-11-02", True),      # only older week delivered
        (vn(2026, 11, 18, 10, 0), "mon 08:00", "2026-11-02", True),   # missed: later in week
        (vn(2026, 11, 22, 23, 0), "mon 08:00", "2026-11-09", False),
        (vn(2026, 11, 22, 23, 0), "mon 08:00", None, True),
        (vn(2026, 11, 19, 12, 0), "fri 17:00", None, False),    # Thursday, report on Friday
        (vn(2026, 11, 20, 17, 0), "fri 17:00", None, True),
        (vn(2026, 11, 20, 17, 0), "fri 17:00", dt.date(2026, 11, 9), False),
        (vn(2026, 11, 20, 17, 0), "fri 17:00", dt.date(2026, 11, 2), True),
        (vn(*MON, 8, 0), "", None, False),                      # disabled
        (vn(*MON, 8, 0), (0, 8, 0), None, True),                # parsed tuple accepted
        (dt.datetime(2026, 11, 16, 1, 0, tzinfo=dt.timezone.utc), "mon 08:00", None, True),
        (dt.datetime(2026, 11, 16, 0, 59, tzinfo=dt.timezone.utc), "mon 08:00", None, False),
    ],
)
def test_due_report(now, report_at, last, expected):
    assert scheduler.due_report(now, report_at, last) is expected


def test_report_week_start_is_previous_monday():
    assert scheduler.report_week_start(vn(*MON, 8, 0)) == dt.date(2026, 11, 9)
    assert scheduler.report_week_start(vn(2026, 11, 22, 23, 59)) == dt.date(2026, 11, 9)
    assert scheduler.report_week_start(vn(2026, 11, 23, 0, 0)) == dt.date(2026, 11, 16)


# ---------------------------------------------------------------------------
# tick()
# ---------------------------------------------------------------------------

class Recorder:
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def __call__(self, con, settings):
        self.calls += 1
        assert con.execute("SELECT 1").fetchone()[0] == 1  # gets an open connection
        if self.fail:
            raise RuntimeError("job exploded")
        return {"ok": True}


def deliver_report(con, settings):
    """Fake send_report: marks the previous week as delivered."""
    monday, _ = core.week_bounds(-1)
    with con:
        con.execute(
            "INSERT INTO reports(week_start, kind, created_at, delivered, body)"
            " VALUES (?, 'weekly', ?, 1, 'x')",
            (monday.isoformat(), config.now_vn().isoformat()),
        )


def make(settings, **kwargs):
    kwargs.setdefault("threaded_jobs", False)
    return scheduler.Scheduler(settings, **kwargs)


def add_run(settings, finished: dt.datetime, collector: str = "yt-dlp"):
    con = db.connect(settings.db)
    try:
        student = core.add_student(con, "Lan", 2, youtube_url="https://www.youtube.com/@lan")
        core.record_sync_run(con, student["id"], "youtube", collector, "ok",
                             finished - dt.timedelta(minutes=1), finished, 30, 0, [])
    finally:
        con.close()


def test_sync_runs_on_startup_when_db_empty_then_every_interval(settings):
    settings.report_at = ""
    sync = Recorder()
    sched = make(settings, sync_fn=sync)
    start = vn(*MON, 9, 0)
    assert sched.tick(start) == ["sync"]
    assert sched.tick(start + dt.timedelta(hours=1)) == []
    assert sched.tick(start + dt.timedelta(hours=5, minutes=59)) == []
    assert sched.tick(start + dt.timedelta(hours=6)) == ["sync"]
    assert sync.calls == 2


def test_sync_skipped_on_startup_when_recent_run(settings):
    settings.report_at = ""
    add_run(settings, vn(*MON, 8, 0))
    sync = Recorder()
    sched = make(settings, sync_fn=sync)
    assert sched.tick(vn(*MON, 9, 0)) == []
    assert sched.tick(vn(*MON, 13, 59)) == []
    assert sched.tick(vn(*MON, 14, 0)) == ["sync"]
    assert sync.calls == 1


def test_sync_runs_on_startup_when_last_run_is_old(settings):
    settings.report_at = ""
    add_run(settings, vn(*MON, 1, 0))
    sync = Recorder()
    assert make(settings, sync_fn=sync).tick(vn(*MON, 9, 0)) == ["sync"]


def test_ingest_runs_do_not_count_as_server_sync(settings):
    settings.report_at = ""
    add_run(settings, vn(*MON, 8, 30), collector="ingest:mac")
    assert make(settings, sync_fn=Recorder()).tick(vn(*MON, 9, 0)) == ["sync"]


def test_sync_disabled(settings):
    settings.report_at = ""
    settings.sync_every_hours = 0
    sync = Recorder()
    sched = make(settings, sync_fn=sync)
    for hours in (0, 6, 12, 100):
        assert sched.tick(vn(*MON, 9, 0) + dt.timedelta(hours=hours)) == []
    assert sync.calls == 0


def test_report_sent_once_per_week(settings, freeze_time):
    settings.sync_every_hours = 0
    report = Recorder()

    def report_fn(con, s):
        report(con, s)
        deliver_report(con, s)

    sched = make(settings, report_fn=report_fn)
    freeze_time(vn(*MON, 7, 0))
    assert sched.tick(vn(*MON, 7, 0)) == []
    freeze_time(vn(*MON, 8, 0))
    assert sched.tick(vn(*MON, 8, 0)) == ["report"]
    assert sched.tick(vn(*MON, 8, 1)) == []
    # A new scheduler (service restart) sees the delivered row and does not resend.
    assert make(settings, report_fn=report_fn).tick(vn(2026, 11, 18, 9, 0)) == []
    # Next week it is due again.
    freeze_time(vn(2026, 11, 23, 8, 0))
    assert sched.tick(vn(2026, 11, 23, 8, 0)) == ["report"]
    assert report.calls == 2


def test_report_sent_late_if_service_was_down(settings, freeze_time):
    settings.sync_every_hours = 0
    report = Recorder()
    freeze_time(vn(2026, 11, 19, 15, 0))
    assert make(settings, report_fn=report).tick(vn(2026, 11, 19, 15, 0)) == ["report"]
    assert report.calls == 1


def test_failed_report_not_retried_in_same_process(settings, caplog):
    settings.sync_every_hours = 0
    report = Recorder(fail=True)
    sched = make(settings, report_fn=report)
    with caplog.at_level(logging.ERROR, logger="video_tracker.scheduler"):
        assert sched.tick(vn(*MON, 8, 0)) == ["report"]
    assert "job exploded" in caplog.text
    assert sched.tick(vn(*MON, 9, 0)) == []
    assert report.calls == 1
    assert sched.tick(vn(2026, 11, 23, 8, 0)) == ["report"]  # next week tries again


def test_job_exception_isolated(settings, caplog):
    sync = Recorder(fail=True)
    report = Recorder()
    sched = make(settings, sync_fn=sync, report_fn=report)
    with caplog.at_level(logging.ERROR, logger="video_tracker.scheduler"):
        assert sched.tick(vn(*MON, 8, 0)) == ["sync", "report"]
    assert sync.calls == 1 and report.calls == 1
    assert "sync" in caplog.text and "RuntimeError" in caplog.text
    assert not sched.is_running("sync")
    assert sched.tick(vn(*MON, 14, 0)) == ["sync"]  # lock was released


def test_no_overlapping_runs_of_same_job(settings):
    settings.report_at = ""
    release = threading.Event()
    entered = threading.Event()
    calls = []

    def slow_sync(con, s):
        calls.append(1)
        entered.set()
        release.wait(5)

    sched = make(settings, sync_fn=slow_sync, threaded_jobs=True)
    assert sched.tick(vn(*MON, 8, 0)) == ["sync"]
    assert entered.wait(5)
    assert sched.is_running("sync")
    assert sched.tick(vn(*MON, 14, 0)) == []  # still running → skipped
    release.set()
    sched.join_jobs()
    assert not sched.is_running("sync")
    assert sched.tick(vn(*MON, 14, 1)) == ["sync"]
    sched.join_jobs()
    assert len(calls) == 2


def test_invalid_report_at_fails_fast(settings):
    settings.report_at = "someday"
    with pytest.raises(ValueError):
        scheduler.Scheduler(settings)


def test_start_and_stop_background_thread(settings):
    settings.report_at = ""
    ran = threading.Event()

    def sync(con, s):
        ran.set()

    sched = scheduler.Scheduler(settings, sync_fn=sync, poll_seconds=0.05)
    sched.start()
    try:
        assert ran.wait(5)
        assert sched._thread is not None and sched._thread.daemon
    finally:
        sched.stop()
    assert sched._thread is None


def test_loop_survives_tick_errors(settings, monkeypatch):
    settings.report_at = ""
    sched = scheduler.Scheduler(settings, sync_fn=Recorder(), poll_seconds=0.01)
    ticks = []
    done = threading.Event()

    def bad_tick(now=None):
        ticks.append(now)
        if len(ticks) >= 3:
            done.set()
        raise RuntimeError("tick failed")

    monkeypatch.setattr(sched, "tick", bad_tick)
    sched.start()
    try:
        assert done.wait(5)
    finally:
        sched.stop()
    assert len(ticks) >= 3
