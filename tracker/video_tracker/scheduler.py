"""Background scheduler: periodic ``sync_all`` and the weekly report.

* Sync runs every ``settings.sync_every_hours`` (0 disables it). On start-up it runs only
  when the newest non-ingest sync run in the DB is older than the interval (or there is
  none), so restarting the service does not trigger a burst of collection.
* After each scheduled sync, sync runs older than 180 days are pruned, except the newest
  successful run per (student, platform, channel_url).
* The weekly report ALWAYS covers the previous Monday–Sunday week (``week_offset=-1``),
  whatever weekday ``report_at`` names (e.g. "wed 08:00" still reports last Mon–Sun), and is sent once the
  current week reaches ``settings.report_at`` (VN time, e.g. ``"mon 08:00"``; empty disables
  it). If the service was down at that moment it is sent at the next tick later in the week.
  The ``reports`` table (delivered=1 plus an atomic claim in send_report) prevents
  duplicates; besides, each week is attempted at
  most once per process, so a failed delivery is NOT retried automatically (avoids double
  sends on ambiguous failures) — it is logged and can be re-sent manually.
* Each job runs in its own worker thread, one run at a time per job; exceptions are logged
  (type and message only, never settings) and never stop the loop.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
from typing import Any, Callable

from . import config, core, db

log = logging.getLogger(__name__)

WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "t2": 0, "t3": 1, "t4": 2, "t5": 3, "t6": 4, "t7": 5, "cn": 6,
}

_REPORT_AT_RE = re.compile(r"([a-z0-9]{2,3})\s+(\d{1,2}):(\d{2})")

POLL_SECONDS = 30.0


def parse_report_at(text: str | None) -> tuple[int, int, int] | None:
    """Parse ``"mon 08:00"`` (also ``t2``..``t7``/``cn``) → (weekday, hour, minute).

    Empty → None (reports disabled). Invalid → ValueError (Vietnamese).
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    match = _REPORT_AT_RE.fullmatch(raw)
    if not match or match.group(1) not in WEEKDAYS:
        raise ValueError(
            "TRACKER_REPORT_AT phải có dạng 'mon 08:00' (mon..sun hoặc t2..t7, cn)."
        )
    hour, minute = int(match.group(2)), int(match.group(3))
    if hour > 23 or minute > 59:
        raise ValueError("Giờ trong TRACKER_REPORT_AT không hợp lệ (00:00–23:59).")
    return WEEKDAYS[match.group(1)], hour, minute


def _as_date(value: Any) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value).strip()[:10])


def report_week_start(now: dt.datetime) -> dt.date:
    """Monday of the week before ``now`` (the week a report sent now covers)."""
    local = now.astimezone(config.TZ) if now.tzinfo else now.replace(tzinfo=config.TZ)
    monday = local.date() - dt.timedelta(days=local.weekday())
    return monday - dt.timedelta(days=7)


def due_report(
    now: dt.datetime,
    report_at: str | tuple[int, int, int] | None,
    last_week_start: str | dt.date | None,
) -> bool:
    """True when the previous week's report should be sent now.

    ``last_week_start`` is the ``week_start`` of the newest DELIVERED weekly report (or None).
    Due when ``now`` is at/after this week's scheduled moment and the previous week has not
    been delivered yet — including later in the week if the scheduled moment was missed.
    """
    schedule = parse_report_at(report_at) if not isinstance(report_at, tuple) else report_at
    if schedule is None:
        return False
    weekday, hour, minute = schedule
    local = now.astimezone(config.TZ) if now.tzinfo else now.replace(tzinfo=config.TZ)
    monday = local.date() - dt.timedelta(days=local.weekday())
    scheduled = dt.datetime.combine(
        monday + dt.timedelta(days=weekday), dt.time(hour, minute), tzinfo=config.TZ
    )
    if local < scheduled:
        return False
    target = monday - dt.timedelta(days=7)
    last = _as_date(last_week_start)
    return last is None or last < target


def _default_sync(con, settings: config.Settings) -> Any:
    from . import collect  # lazy: collect.py is owned by another module

    summary = collect.sync_all(con, settings)
    pruned = core.prune_sync_runs(con, core.PRUNE_DAYS)
    if pruned:
        log.info("Scheduler: đã xóa %d lần đồng bộ cũ hơn %d ngày.", pruned, core.PRUNE_DAYS)
    return summary


def _default_report(con, settings: config.Settings) -> Any:
    from . import report  # lazy: report.py is owned by another module

    return report.send_report(con, settings, week_offset=-1)


class Scheduler:
    """Daemon-thread scheduler; ``tick(now)`` is the testable unit."""

    def __init__(
        self,
        settings: config.Settings,
        sync_fn: Callable[[Any, config.Settings], Any] | None = None,
        report_fn: Callable[[Any, config.Settings], Any] | None = None,
        poll_seconds: float = POLL_SECONDS,
        threaded_jobs: bool = True,
    ) -> None:
        self.settings = settings
        self.sync_fn = sync_fn or _default_sync
        self.report_fn = report_fn or _default_report
        self.poll_seconds = poll_seconds
        self.threaded_jobs = threaded_jobs
        self.report_at = parse_report_at(settings.report_at)  # fail fast on bad config
        hours = float(settings.sync_every_hours or 0)
        self.sync_interval = dt.timedelta(hours=hours) if hours > 0 else None
        self.next_sync_at: dt.datetime | None = None
        self.attempted_weeks: set[dt.date] = set()
        self._locks = {"sync": threading.Lock(), "report": threading.Lock()}
        self._workers: dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- DB lookups -------------------------------------------------------

    def _query(self, sql: str) -> Any:
        con = db.connect(self.settings.db)
        try:
            row = con.execute(sql).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def last_sync_at(self) -> dt.datetime | None:
        """finished_at of the newest server-side (non-ingest) sync run."""
        newest = None
        con = db.connect(self.settings.db)
        try:
            for (finished,) in con.execute(
                "SELECT finished_at FROM sync_runs WHERE collector NOT LIKE 'ingest:%'"
            ):
                moment = core.parse_timestamp(finished)
                if newest is None or moment > newest:
                    newest = moment
        finally:
            con.close()
        return newest

    def last_delivered_week(self) -> str | None:
        return self._query(
            "SELECT max(week_start) FROM reports WHERE kind = 'weekly' AND delivered = 1"
        )

    # -- jobs ---------------------------------------------------------------

    def _job_body(self, name: str, fn: Callable[[Any, config.Settings], Any]) -> None:
        lock = self._locks[name]
        try:
            con = db.connect(self.settings.db)
            try:
                fn(con, self.settings)
            finally:
                con.close()
            log.info("Scheduler: job %s xong.", name)
        except Exception as exc:  # isolate: a failing job never kills the loop
            log.error("Scheduler: job %s lỗi: %s: %s", name, type(exc).__name__, exc)
        finally:
            lock.release()

    def _launch(self, name: str, fn: Callable[[Any, config.Settings], Any]) -> bool:
        """Start a job unless the same job is still running. Returns True if started."""
        if not self._locks[name].acquire(blocking=False):
            log.info("Scheduler: job %s đang chạy, bỏ qua lượt này.", name)
            return False
        if self.threaded_jobs:
            worker = threading.Thread(
                target=self._job_body, args=(name, fn), name=f"tracker-{name}", daemon=True
            )
            self._workers[name] = worker
            worker.start()
        else:
            self._job_body(name, fn)
        return True

    def is_running(self, name: str) -> bool:
        return self._locks[name].locked()

    # -- scheduling -------------------------------------------------------------

    def tick(self, now: dt.datetime | None = None) -> list[str]:
        """Run whatever is due at ``now``; return the names of jobs started."""
        now = now or config.now_vn()
        started: list[str] = []

        if self.sync_interval is not None:
            if self.next_sync_at is None:
                try:
                    last = self.last_sync_at()
                except Exception as exc:
                    log.error("Scheduler: không đọc được lịch sử sync: %s", type(exc).__name__)
                    last = None
                # Start-up: sync now only if the last run is older than the interval.
                self.next_sync_at = now if last is None else max(now, last + self.sync_interval)
            if now >= self.next_sync_at and self._launch("sync", self.sync_fn):
                started.append("sync")
                self.next_sync_at = now + self.sync_interval

        if self.report_at is not None:
            target = report_week_start(now)
            try:
                last_week = self.last_delivered_week()
            except Exception as exc:
                log.error("Scheduler: không đọc được bảng reports: %s", type(exc).__name__)
                last_week = None
            if (
                target not in self.attempted_weeks
                and due_report(now, self.report_at, last_week)
                and self._launch("report", self.report_fn)
            ):
                self.attempted_weeks.add(target)
                started.append("report")
        return started

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(config.now_vn())
            except Exception as exc:
                log.error("Scheduler: lỗi vòng lặp: %s: %s", type(exc).__name__, exc)
            self._stop.wait(self.poll_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tracker-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def join_jobs(self, timeout: float = 5.0) -> None:
        """Wait for running job threads (used by tests and shutdown)."""
        for worker in list(self._workers.values()):
            worker.join(timeout)
