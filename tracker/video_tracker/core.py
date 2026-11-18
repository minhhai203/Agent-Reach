"""Domain logic: students, videos, sync runs and the weekly status rules."""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from typing import Any, Iterable

from . import config, urls

PLATFORMS = ("tiktok", "youtube")
SOURCES = ("manual", "observed", "api", "reach")
RUN_STATUSES = ("ok", "partial", "failed")
STATUSES = ("met", "at_least", "below_goal", "insufficient_data")

# Pinned TikTok videos can be old, so a few "older" dates do not prove we read past the week.
OLDER_GUARD = {"tiktok": 3, "youtube": 0}

MAX_NAME = 100
MAX_TITLE = 300
MAX_ERROR = 500
MAX_HISTORY_WEEKS = 52
MAX_ID = 2**63 - 1  # SQLite INTEGER range; larger ids can never exist
MAX_SEGMENTS = 4
PRUNE_DAYS = 180
ERR_FOREIGN = "{n} video đã được ghi nhận cho học viên khác"
_SEGMENT_NAME_RE = re.compile(r"[a-z]{1,16}")

DISCLAIMER = (
    "Số liệu dựa trên dữ liệu thu thập được; kênh do giáo viên khai báo, chưa xác minh "
    "chủ sở hữu. Khi dữ liệu chưa đủ thì không kết luận về việc đăng video của học viên."
)

STATUS_LABELS = {
    "met": "Đủ mục tiêu",
    "at_least": "Ít nhất {count} video (có thể còn video chưa đọc tới)",
    "below_goal": "Thiếu theo dữ liệu hiện có",
    "insufficient_data": "Chưa đủ dữ liệu để kết luận",
}

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CODE_RE = re.compile(r"S(\d+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return config.now_vn().isoformat(timespec="seconds")


def _today() -> dt.date:
    return config.now_vn().date()


def parse_date(value: Any) -> dt.date:
    """Parse a strict YYYY-MM-DD string (or date) into a date."""
    if isinstance(value, dt.datetime):
        return value.astimezone(config.TZ).date() if value.tzinfo else value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    if not _DATE_RE.fullmatch(text):
        raise ValueError("Ngày đăng phải theo định dạng YYYY-MM-DD.")
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError("Ngày đăng phải theo định dạng YYYY-MM-DD.") from None


def _past_or_today(value: Any) -> str:
    day = parse_date(value)
    if day > _today():
        raise ValueError("Ngày đăng không thể ở tương lai.")
    return day.isoformat()


def parse_timestamp(value: Any) -> dt.datetime:
    """Parse an ISO 8601 timestamp (or datetime) into a VN-aware datetime."""
    if isinstance(value, dt.datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            moment = dt.datetime.fromisoformat(text)
        except ValueError:
            raise ValueError("Thời điểm phải theo định dạng ISO 8601.") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=config.TZ)
    return moment.astimezone(config.TZ)


def _validate_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or len(name) > MAX_NAME:
        raise ValueError(f"Nhập tên học viên (tối đa {MAX_NAME} ký tự).")
    return name


def _validate_goal(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Mục tiêu phải là số nguyên từ 1 đến 50.")
    try:
        goal = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise ValueError("Mục tiêu phải là số nguyên từ 1 đến 50.") from None
    if isinstance(value, float) and value != goal:
        raise ValueError("Mục tiêu phải là số nguyên từ 1 đến 50.")
    if not 1 <= goal <= 50:
        raise ValueError("Mục tiêu phải từ 1 đến 50.")
    return goal


def _validate_channel(platform: str, value: Any) -> str:
    text = str(value or "").strip()
    return urls.canonical_channel(platform, text) if text else ""


def _validate_active(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1):
        return int(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes"):
        return 1
    if text in ("0", "false", "no"):
        return 0
    raise ValueError("Trạng thái hoạt động phải là true hoặc false.")


def _validate_countable(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, bool):
        raise ValueError("Trường countable phải là true hoặc false.")
    return value


def _validate_platform(platform: Any) -> str:
    if platform not in PLATFORMS:
        raise ValueError("Nền tảng không hợp lệ (chỉ tiktok hoặc youtube).")
    return platform


def _validate_source(source: Any) -> str:
    if source not in SOURCES:
        raise ValueError("Nguồn dữ liệu không hợp lệ (manual, observed, api, reach).")
    return source


def _student_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "weekly_goal": row["weekly_goal"],
        "tiktok_url": row["tiktok_url"],
        "youtube_url": row["youtube_url"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
    }


def _video_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "student_id": row["student_id"],
        "platform": row["platform"],
        "video_key": row["video_key"],
        "url": row["url"],
        "title": row["title"],
        "published_date": row["published_date"],
        "source": row["source"],
        "countable": bool(row["countable"]),
        "created_at": row["created_at"],
    }


def _next_code(con: sqlite3.Connection) -> str:
    highest = 0
    for (code,) in con.execute("SELECT code FROM students"):
        match = _CODE_RE.fullmatch(code or "")
        if match:
            highest = max(highest, int(match.group(1)))
    return f"S{highest + 1:02d}"


# ---------------------------------------------------------------------------
# Students
# ---------------------------------------------------------------------------

def add_student(
    con: sqlite3.Connection,
    name: str,
    weekly_goal: int = 3,
    tiktok_url: str = "",
    youtube_url: str = "",
) -> dict:
    """Create a student with an auto-assigned code ('S01', 'S02', …)."""
    clean_name = _validate_name(name)
    goal = _validate_goal(weekly_goal)
    tiktok = _validate_channel("tiktok", tiktok_url)
    youtube = _validate_channel("youtube", youtube_url)
    with con:
        code = _next_code(con)
        cur = con.execute(
            "INSERT INTO students(code, name, weekly_goal, tiktok_url, youtube_url, active, created_at)"
            " VALUES (?, ?, ?, ?, ?, 1, ?)",
            (code, clean_name, goal, tiktok, youtube, _now_iso()),
        )
    return get_student(con, cur.lastrowid)


_UPDATE_VALIDATORS = {
    "name": _validate_name,
    "weekly_goal": _validate_goal,
    "tiktok_url": lambda v: _validate_channel("tiktok", v),
    "youtube_url": lambda v: _validate_channel("youtube", v),
    "active": _validate_active,
}


def update_student(con: sqlite3.Connection, student_id: int | str, **fields: Any) -> dict:
    """Update name, weekly_goal, tiktok_url, youtube_url and/or active."""
    student = get_student(con, student_id)
    unknown = sorted(set(fields) - set(_UPDATE_VALIDATORS))
    if unknown:
        raise ValueError(f"Trường không hợp lệ: {', '.join(unknown)}.")
    values = {key: _UPDATE_VALIDATORS[key](value) for key, value in fields.items()}
    if values:
        assignments = ", ".join(f"{key} = ?" for key in values)
        with con:
            con.execute(
                f"UPDATE students SET {assignments} WHERE id = ?",
                (*values.values(), student["id"]),
            )
    return get_student(con, student["id"])


def list_students(con: sqlite3.Connection, include_inactive: bool = False) -> list[dict]:
    """List students ordered by id (active only unless include_inactive)."""
    sql = "SELECT * FROM students"
    if not include_inactive:
        sql += " WHERE active = 1"
    return [_student_dict(row) for row in con.execute(sql + " ORDER BY id")]


def get_student(con: sqlite3.Connection, student_ref: int | str) -> dict:
    """Find a student by numeric id or code ('S01', case-insensitive)."""
    row = None
    if isinstance(student_ref, int) and not isinstance(student_ref, bool):
        if 0 < student_ref <= MAX_ID:
            row = con.execute("SELECT * FROM students WHERE id = ?", (student_ref,)).fetchone()
    elif isinstance(student_ref, str) and student_ref.strip() and len(student_ref) <= 64:
        text = student_ref.strip()
        if text.isdigit():
            number = int(text)
            if 0 < number <= MAX_ID:
                row = con.execute("SELECT * FROM students WHERE id = ?", (number,)).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM students WHERE upper(code) = ?", (text.upper(),)
            ).fetchone()
    if row is None:
        raise ValueError("Không tìm thấy học viên.")
    return _student_dict(row)


# ---------------------------------------------------------------------------
# Videos and sync runs
# ---------------------------------------------------------------------------

def record_video(
    con: sqlite3.Connection,
    student_id: int | str,
    url: str,
    published_date: str,
    title: str = "",
    source: str = "manual",
) -> dict:
    """Record one video link with its publish date; duplicates raise ValueError.

    A key that exists only as an unreadable post (countable=0) of the SAME student is
    upgraded in place to a countable video with this source/date/title.
    """
    student = get_student(con, student_id)
    platform, video_key, canonical_url = urls.canonical_video(url)
    day = _past_or_today(published_date)
    clean_title = str(title or "").strip()[:MAX_TITLE]
    _validate_source(source)
    exists = con.execute(
        "SELECT id, student_id, countable FROM videos WHERE platform = ? AND video_key = ?",
        (platform, video_key),
    ).fetchone()
    if exists:
        if exists["countable"] or exists["student_id"] != student["id"]:
            raise ValueError("Liên kết này đã được ghi nhận.")
        with con:
            con.execute(
                "UPDATE videos SET countable = 1, url = ?, title = ?, published_date = ?,"
                " source = ? WHERE id = ?",
                (canonical_url, clean_title, day, source, exists["id"]),
            )
        row = con.execute("SELECT * FROM videos WHERE id = ?", (exists["id"],)).fetchone()
        return _video_dict(row)
    try:
        with con:
            cur = con.execute(
                "INSERT INTO videos(student_id, platform, video_key, url, title, published_date,"
                " source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (student["id"], platform, video_key, canonical_url, clean_title, day, source,
                 _now_iso()),
            )
    except sqlite3.IntegrityError:
        raise ValueError("Liên kết này đã được ghi nhận.") from None
    row = con.execute("SELECT * FROM videos WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _video_dict(row)


def upsert_observed_videos(
    con: sqlite3.Connection,
    student_id: int | str,
    platform: str,
    videos: Iterable[dict],
    source: str,
) -> dict:
    """INSERT OR IGNORE collected videos.

    Returns ``{"added": <new rows>, "foreign": <keys already stored for ANOTHER student>}``.
    A foreign key is never re-assigned; callers must treat the collection as incomplete
    for this student (the run becomes ``partial``) because those videos are not counted.

    Each video may carry `countable` (default True). A countable video whose key already
    exists as an unreadable post (countable=0) of the same student upgrades that row
    (countable=1, title filled); an upgrade is not counted as a new row.
    """
    student = get_student(con, student_id)
    _validate_platform(platform)
    _validate_source(source)
    rows = []
    for video in videos:
        key = str(video.get("video_key") or "").strip()
        link = str(video.get("url") or "").strip()
        if not key or not link:
            raise ValueError("Mỗi video cần có video_key và url.")
        day = parse_date(video.get("published_date")).isoformat()
        title = str(video.get("title") or "").strip()[:MAX_TITLE]
        countable = 1 if _validate_countable(video.get("countable")) else 0
        rows.append((student["id"], platform, key, link, title, day, source, _now_iso(),
                     countable))
    added = 0
    foreign: list[str] = []
    with con:
        for row in rows:
            cur = con.execute(
                "INSERT OR IGNORE INTO videos(student_id, platform, video_key, url, title,"
                " published_date, source, created_at, countable)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            added += cur.rowcount
            if cur.rowcount:
                continue
            owner = con.execute(
                "SELECT student_id FROM videos WHERE platform = ? AND video_key = ?",
                (platform, row[2]),
            ).fetchone()
            if owner is not None and owner["student_id"] != student["id"]:
                if row[2] not in foreign:
                    foreign.append(row[2])
                continue
            if row[8] == 1:
                con.execute(
                    "UPDATE videos SET countable = 1, title = ? WHERE platform = ?"
                    " AND video_key = ? AND student_id = ? AND countable = 0",
                    (row[4], platform, row[2], student["id"]),
                )
    return {"added": added, "foreign": foreign}


def foreign_error(count: int) -> str:
    """Vietnamese run error for `count` videos that belong to another student."""
    return ERR_FOREIGN.format(n=int(count))


def _validate_segments(segments: Any, fetch_limit: int) -> list[dict]:
    """Normalise per-tab segments: [{"name","fetched_count","dates"}] (≤ MAX_SEGMENTS)."""
    if segments is None:
        return []
    if not isinstance(segments, (list, tuple)):
        raise ValueError("segments phải là danh sách.")
    if len(segments) > MAX_SEGMENTS:
        raise ValueError(f"Tối đa {MAX_SEGMENTS} segment mỗi lần đồng bộ.")
    out: list[dict] = []
    names: set[str] = set()
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("Mỗi segment phải là một đối tượng.")
        name = segment.get("name")
        if not isinstance(name, str) or not _SEGMENT_NAME_RE.fullmatch(name):
            raise ValueError("Tên segment chỉ gồm a-z (1–16 ký tự).")
        if name in names:
            raise ValueError("Tên segment bị trùng.")
        names.add(name)
        count = segment.get("fetched_count")
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= max(
            fetch_limit, 0
        ) * 2:
            raise ValueError("fetched_count của segment phải là số nguyên từ 0 đến 2×fetch_limit.")
        dates = segment.get("dates", [])
        if not isinstance(dates, (list, tuple)):
            raise ValueError("dates của segment phải là danh sách.")
        if len(dates) > count:
            raise ValueError("Số ngày của segment không được lớn hơn fetched_count.")
        out.append({"name": name, "fetched_count": count,
                    "dates": [parse_date(d).isoformat() for d in dates]})
    return out


def record_sync_run(
    con: sqlite3.Connection,
    student_id: int | str,
    platform: str,
    collector: str,
    status: str,
    started_at: str | dt.datetime,
    finished_at: str | dt.datetime,
    fetch_limit: int,
    fetched_count: int,
    dates: Iterable[Any],
    error: str = "",
    *,
    channel_url: str = "",
    segments: list[dict] | None = None,
) -> int:
    """Store one collection attempt; timestamps are normalised to VN ISO 8601.

    `channel_url` is the channel actually read (coverage only trusts runs whose channel
    equals the student's CURRENT channel; '' never matches). `segments` are optional
    per-tab results ``[{"name","fetched_count","dates"}]`` judged separately by coverage.
    """
    student = get_student(con, student_id)
    _validate_platform(platform)
    collector_name = str(collector or "").strip()
    if not collector_name:
        raise ValueError("Thiếu tên bộ thu thập (collector).")
    if status not in RUN_STATUSES:
        raise ValueError("Trạng thái đồng bộ không hợp lệ (ok, partial, failed).")
    started = parse_timestamp(started_at)
    finished = parse_timestamp(finished_at)
    try:
        limit = int(fetch_limit)
        count = int(fetched_count)
    except (TypeError, ValueError):
        raise ValueError("fetch_limit và fetched_count phải là số nguyên.") from None
    if limit < 0 or count < 0:
        raise ValueError("fetch_limit và fetched_count không được âm.")
    date_list = [parse_date(d).isoformat() for d in dates]
    channel = str(channel_url or "").strip()
    if len(channel) > urls.MAX_URL_LENGTH:
        raise ValueError(urls.ERR_TOO_LONG)
    segment_list = _validate_segments(segments, limit)
    with con:
        cur = con.execute(
            "INSERT INTO sync_runs(student_id, platform, collector, status, started_at,"
            " finished_at, fetch_limit, fetched_count, dates_json, error, channel_url,"
            " segments_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                student["id"], platform, collector_name, status,
                started.isoformat(timespec="seconds"), finished.isoformat(timespec="seconds"),
                limit, count, json.dumps(date_list), str(error or "")[:MAX_ERROR],
                channel, json.dumps(segment_list),
            ),
        )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Weeks and status rules
# ---------------------------------------------------------------------------

def week_bounds(week_offset: int = 0) -> tuple[dt.date, dt.date]:
    """Monday and Sunday of the week `week_offset` weeks from now (VN time)."""
    today = _today()
    monday = today - dt.timedelta(days=today.weekday()) + dt.timedelta(weeks=int(week_offset))
    return monday, monday + dt.timedelta(days=6)


RunIndex = dict[tuple[int, str, str], list[sqlite3.Row]]


def _runs_by_student_platform(con: sqlite3.Connection) -> RunIndex:
    """All sync runs grouped by (student_id, platform, channel_url), newest first.

    Lookups use the student's CURRENT channel URL, so runs of a previous channel (and
    pre-v3 runs, stored with channel_url '') are never used as evidence.
    """
    grouped: RunIndex = {}
    for row in con.execute("SELECT * FROM sync_runs ORDER BY finished_at DESC, id DESC"):
        grouped.setdefault(
            (row["student_id"], row["platform"], row["channel_url"]), []
        ).append(row)
    return grouped


def _runs_for(runs: RunIndex, student: dict, platform: str) -> list[sqlite3.Row]:
    channel = student.get(f"{platform}_url") or ""
    if not channel:
        return []
    return runs.get((student["id"], platform, channel), [])


def _json_list(text: Any) -> list:
    try:
        value = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _part_complete(platform: str, fetched_count: int, fetch_limit: int, dates: list,
                   monday: dt.date) -> bool:
    """True when one listing (whole run or one tab) is known to reach before `monday`."""
    older = sum(1 for d in dates if str(d) < monday.isoformat())
    return int(fetched_count) < int(fetch_limit) or older > OLDER_GUARD[platform]


def _coverage(
    platform: str,
    channel_url: str,
    runs: list[sqlite3.Row],
    monday: dt.date,
    needed_after: dt.datetime,
) -> dict:
    """Coverage of one platform for one week, following the status rules."""
    latest = runs[0] if runs else None
    successes = [r for r in runs if r["status"] in ("ok", "partial")]
    entry = {
        "channel_url": channel_url,
        "coverage": "unknown",
        "reason": "",
        "last_synced_at": successes[0]["finished_at"] if successes else "",
        "last_status": latest["status"] if latest else "",
        "last_error": latest["error"] if latest else "",
        "segments": [],
    }
    if latest is not None and latest["status"] == "failed":
        entry["reason"] = "last_sync_failed"
        return entry
    fresh = next(
        (r for r in successes if parse_timestamp(r["finished_at"]) >= needed_after), None
    )
    if fresh is None:
        if latest is None:
            entry["reason"] = "never_synced"
        else:
            entry["reason"] = "stale"
        return entry
    segments = [seg for seg in _json_list(fresh["segments_json"]) if isinstance(seg, dict)]
    if segments:
        # Each tab (e.g. YouTube /videos and /shorts) must be read past the week on its own;
        # the worst tab decides.
        judged = []
        for seg in segments:
            seg_complete = _part_complete(
                platform, seg.get("fetched_count", 0), fresh["fetch_limit"],
                seg.get("dates") or [], monday,
            )
            judged.append({
                "name": str(seg.get("name", "")),
                "fetched_count": int(seg.get("fetched_count", 0)),
                "coverage": "complete" if seg_complete else "lower_bound",
            })
        entry["segments"] = judged
        complete = all(item["coverage"] == "complete" for item in judged)
    else:
        complete = _part_complete(
            platform, fresh["fetched_count"], fresh["fetch_limit"],
            _json_list(fresh["dates_json"]), monday,
        )
    if complete and fresh["status"] == "ok":
        entry["coverage"] = "complete"
    else:
        entry["coverage"] = "lower_bound"
    return entry


def _student_week(
    con: sqlite3.Connection,
    student: dict,
    monday: dt.date,
    sunday: dt.date,
    is_current: bool,
    now: dt.datetime,
    fresh_hours: float,
    runs: RunIndex,
) -> dict:
    """Compute one student's entry for one week."""
    end_day = min(sunday, now.date())
    rows = con.execute(
        "SELECT * FROM videos WHERE student_id = ? AND published_date >= ? AND published_date <= ?"
        " ORDER BY published_date, id",
        (student["id"], monday.isoformat(), end_day.isoformat()),
    ).fetchall()
    countable_rows = [row for row in rows if row["countable"]]
    count = len(countable_rows)
    unreadable_posts = len(rows) - count
    by_source = {source: 0 for source in SOURCES}
    for row in countable_rows:
        by_source[row["source"]] += 1

    if is_current:
        needed_after = now - dt.timedelta(hours=fresh_hours)
    else:
        needed_after = dt.datetime.combine(
            sunday + dt.timedelta(days=1), dt.time(0, 0), tzinfo=config.TZ
        )

    platforms: dict[str, dict] = {}
    for platform in PLATFORMS:
        channel = student[f"{platform}_url"]
        if channel:
            platforms[platform] = _coverage(
                platform, channel, _runs_for(runs, student, platform), monday, needed_after
            )

    reasons: list[str] = []
    unknown = [p for p, info in platforms.items() if info["coverage"] == "unknown"]
    lower = [p for p, info in platforms.items() if info["coverage"] == "lower_bound"]
    if not platforms:
        status = "insufficient_data"
        reasons = ["no_channel"]
    elif unknown:
        status = "insufficient_data"
        reasons = [f"{p}:{platforms[p]['reason']}" for p in unknown]
    elif count >= student["weekly_goal"]:
        status = "met"
    elif lower:
        status = "at_least"
        reasons = [f"{p}:lower_bound" for p in lower]
    else:
        status = "below_goal"

    return {
        "id": student["id"],
        "code": student["code"],
        "name": student["name"],
        "weekly_goal": student["weekly_goal"],
        "count": count,
        "unreadable_posts": unreadable_posts,
        "status": status,
        "status_label": STATUS_LABELS[status].format(count=count),
        "reasons": reasons,
        "by_source": by_source,
        "platforms": platforms,
        "videos": [
            {
                "platform": row["platform"],
                "url": row["url"],
                "title": row["title"],
                "published_date": row["published_date"],
                "source": row["source"],
                "countable": bool(row["countable"]),
            }
            for row in rows
        ],
    }


def _fresh_hours(settings: config.Settings | None) -> float:
    return float((settings or config.load_settings()).fresh_hours)


def week_progress(
    con: sqlite3.Connection, week_offset: int = 0, settings: config.Settings | None = None
) -> dict:
    """Weekly progress of all active students (week_offset <= 0)."""
    offset = int(week_offset)
    if offset > 0:
        raise ValueError("Không thể xem tiến độ của tuần trong tương lai.")
    fresh_hours = _fresh_hours(settings)
    now = config.now_vn()
    monday, sunday = week_bounds(offset)
    runs = _runs_by_student_platform(con)
    entries = [
        _student_week(con, s, monday, sunday, offset == 0, now, fresh_hours, runs)
        for s in list_students(con)
    ]
    summary = {"students": len(entries)}
    for status in STATUSES:
        summary[status] = sum(1 for e in entries if e["status"] == status)
    return {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "is_current": offset == 0,
        "generated_at": now.isoformat(timespec="seconds"),
        "timezone": "Asia/Ho_Chi_Minh",
        "summary": summary,
        "students": entries,
        "disclaimer": DISCLAIMER,
    }


def student_history(
    con: sqlite3.Connection,
    student_ref: int | str,
    weeks: int = 4,
    settings: config.Settings | None = None,
) -> dict:
    """Per-week entries for one student, from the current week backwards."""
    student = get_student(con, student_ref)
    try:
        count = int(weeks)
    except (TypeError, ValueError):
        raise ValueError(f"Số tuần phải là số nguyên từ 1 đến {MAX_HISTORY_WEEKS}.") from None
    if not 1 <= count <= MAX_HISTORY_WEEKS:
        raise ValueError(f"Số tuần phải là số nguyên từ 1 đến {MAX_HISTORY_WEEKS}.")
    fresh_hours = _fresh_hours(settings)
    now = config.now_vn()
    runs = _runs_by_student_platform(con)
    result = []
    for offset in range(0, -count, -1):
        monday, sunday = week_bounds(offset)
        entry = _student_week(con, student, monday, sunday, offset == 0, now, fresh_hours, runs)
        entry["week_start"] = monday.isoformat()
        entry["week_end"] = sunday.isoformat()
        entry["is_current"] = offset == 0
        result.append(entry)
    return {"student": student, "weeks": result, "disclaimer": DISCLAIMER}


def sync_status(con: sqlite3.Connection) -> list[dict]:
    """Latest sync run per active student × declared platform."""
    runs = _runs_by_student_platform(con)
    result = []
    for student in list_students(con):
        for platform in PLATFORMS:
            channel = student[f"{platform}_url"]
            if not channel:
                continue
            history = _runs_for(runs, student, platform)
            latest = history[0] if history else None
            success = next((r for r in history if r["status"] in ("ok", "partial")), None)
            result.append({
                "student_id": student["id"],
                "code": student["code"],
                "name": student["name"],
                "platform": platform,
                "channel_url": channel,
                "synced": latest is not None,
                "collector": latest["collector"] if latest else "",
                "status": latest["status"] if latest else "",
                "started_at": latest["started_at"] if latest else "",
                "finished_at": latest["finished_at"] if latest else "",
                "fetch_limit": latest["fetch_limit"] if latest else 0,
                "fetched_count": latest["fetched_count"] if latest else 0,
                "error": latest["error"] if latest else "",
                "last_success_at": success["finished_at"] if success else "",
            })
    return result


def prune_sync_runs(con: sqlite3.Connection, days: int = PRUNE_DAYS) -> int:
    """Delete sync runs finished more than `days` days ago; return how many were deleted.

    The newest successful run (ok/partial) per (student, platform, channel_url) is always
    kept, whatever its age, so a quiet channel keeps its last evidence.
    """
    cutoff = (config.now_vn() - dt.timedelta(days=int(days))).isoformat(timespec="seconds")
    with con:
        cur = con.execute(
            "DELETE FROM sync_runs WHERE finished_at < ? AND id NOT IN ("
            " SELECT id FROM (SELECT id, ROW_NUMBER() OVER ("
            "   PARTITION BY student_id, platform, channel_url"
            "   ORDER BY finished_at DESC, id DESC) AS rn"
            "  FROM sync_runs WHERE status IN ('ok', 'partial')) WHERE rn = 1)",
            (cutoff,),
        )
    return cur.rowcount
