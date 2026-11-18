"""Per-video observations and changes between adjacent calendar periods in Vietnam."""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import config, core

FIELDS = ("view_count", "like_count", "comment_count")
MAX_VALUE = 2**63 - 1


def from_collector(entry: dict) -> dict:
    """Unusable platform values are missing; zero is a valid measurement."""
    return {field: value if type(value := entry.get(field)) is int and 0 <= value <= MAX_VALUE
            else None for field in FIELDS}


def validate(value: Any) -> dict:
    """Validate an untrusted ingest observation without coercing values."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(FIELDS):
        raise ValueError("metrics phải là đối tượng view_count, like_count, comment_count.")
    for field in FIELDS:
        number = value.get(field)
        if number is not None and (type(number) is not int or not 0 <= number <= MAX_VALUE):
            raise ValueError(f"metrics.{field} phải là số nguyên không âm hoặc null.")
    return {field: value.get(field) for field in FIELDS}


def record_snapshots(con, student_id: int, platform: str, videos: list[dict],
                     observed_at: Any, sync_run_id: int) -> None:
    """Record one observation per owned video and second; repeated pushes are idempotent."""
    observed = core.parse_timestamp(observed_at).isoformat(timespec="seconds")
    with con:
        for video in videos:
            if not video.get("countable", True):
                continue
            values = validate(video.get("metrics"))
            con.execute(
                "INSERT OR IGNORE INTO video_metric_snapshots"
                " (video_id, sync_run_id, observed_at, view_count, like_count, comment_count)"
                " SELECT id, ?, ?, ?, ?, ? FROM videos"
                " WHERE student_id = ? AND platform = ? AND video_key = ? AND countable = 1",
                (sync_run_id, observed, *(values[f] for f in FIELDS), student_id, platform,
                 video["video_key"]),
            )


def history(con, student_ref: str, period: str = "day", count: int = 7, offset: int = 0) -> dict:
    """Changes between latest observations in adjacent calendar periods, not interpolation."""
    if period not in ("day", "week"):
        raise ValueError("period phải là day hoặc week.")
    if type(count) is not int or not 1 <= count <= 52:
        raise ValueError("count phải từ 1 đến 52.")
    if type(offset) is not int or not 0 <= offset <= 5200:
        raise ValueError("offset phải từ 0 đến 5200.")
    student = core.get_student(con, student_ref)
    now = config.now_vn()
    today = now.date()
    step = dt.timedelta(days=1 if period == "day" else 7)
    current = (today if period == "day" else today - dt.timedelta(days=today.weekday())) - offset * step
    first = current - count * step
    videos = con.execute(
        "SELECT id, platform, url, title, published_date FROM videos"
        " WHERE student_id = ? AND countable = 1 ORDER BY published_date DESC, id DESC",
        (student["id"],),
    ).fetchall()
    rows = con.execute(
        "SELECT m.* FROM video_metric_snapshots m JOIN videos v ON v.id = m.video_id"
        " WHERE v.student_id = ? AND m.observed_at >= ? AND m.observed_at < ? AND m.observed_at <= ?"
        " ORDER BY m.observed_at, m.id",
        (student["id"], dt.datetime.combine(first, dt.time.min, tzinfo=config.TZ).isoformat(),
         dt.datetime.combine(current + step, dt.time.min, tzinfo=config.TZ).isoformat(),
         now.isoformat(timespec="seconds")),
    ).fetchall()
    latest: dict[tuple[int, dt.date], dict] = {}
    for row in rows:
        day = core.parse_timestamp(row["observed_at"]).date()
        start = day if period == "day" else day - dt.timedelta(days=day.weekday())
        latest[(row["video_id"], start)] = dict(row)
    result = []
    for video in videos:
        periods = []
        for offset in range(count):
            start = current - offset * step
            current_row = latest.get((video["id"], start))
            previous_row = latest.get((video["id"], start - step))
            values = {f: current_row[f] if current_row else None for f in FIELDS}
            delta = {}
            for f in FIELDS:
                previous = previous_row[f] if previous_row else None
                delta[f] = values[f] - previous if values[f] is not None and previous is not None else None
            periods.append({"start": start.isoformat(), "end": (start + step - dt.timedelta(days=1)).isoformat(),
                            "observed_at": current_row["observed_at"] if current_row else None,
                            "baseline_at": previous_row["observed_at"] if previous_row else None,
                            "values": values, "change": delta})
        result.append({**dict(video), "periods": periods})
    return {"student": student, "period": period, "offset": offset, "timezone": "Asia/Ho_Chi_Minh",
            "generated_at": now.isoformat(timespec="seconds"), "videos": result}
