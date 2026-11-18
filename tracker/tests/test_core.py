import datetime as dt
import json

import pytest

from conftest import vn
from video_tracker import config, core

TT = "https://www.tiktok.com/@lan"
YT = "https://www.youtube.com/@lan"
# Wednesday; current week is Mon 2026-11-16 .. Sun 2026-11-22.
NOW = vn(2026, 11, 18, 12, 0)


@pytest.fixture(autouse=True)
def frozen(freeze_time):
    return freeze_time(NOW)


def tiktok_url(n: int) -> str:
    return f"https://www.tiktok.com/@lan/video/{7300000000000000000 + n}"


def youtube_url(n: int) -> str:
    return f"https://www.youtube.com/watch?v=abcdefghi{n:02d}"


def run(con, student, platform="tiktok", status="ok", finished=None, limit=30,
        fetched=5, dates=(), error="", channel_url=None, segments=None):
    finished = finished or NOW - dt.timedelta(hours=1)
    if channel_url is None:  # the student's current channel (as collectors record it)
        channel_url = core.get_student(con, student["id"])[f"{platform}_url"]
    return core.record_sync_run(
        con, student["id"], platform, "yt-dlp", status,
        finished - dt.timedelta(minutes=1), finished, limit, fetched, list(dates), error,
        channel_url=channel_url, segments=segments,
    )


def entry(progress, student):
    return next(s for s in progress["students"] if s["id"] == student["id"])


def progress_of(con, settings, student, week_offset=0):
    return entry(core.week_progress(con, week_offset, settings), student)


# ---------------------------------------------------------------------------
# Students
# ---------------------------------------------------------------------------

def test_add_student_and_codes(con):
    a = core.add_student(con, "  Lan  ", 3, tiktok_url="https://m.tiktok.com/@lan/")
    b = core.add_student(con, "Minh", "5", youtube_url="https://youtube.com/@minh/videos")
    assert a["code"] == "S01" and b["code"] == "S02"
    assert a["name"] == "Lan" and a["tiktok_url"] == TT and a["youtube_url"] == ""
    assert b["weekly_goal"] == 5 and b["youtube_url"] == "https://www.youtube.com/@minh"
    assert a["active"] is True
    assert a["created_at"] == NOW.isoformat(timespec="seconds")


def test_codes_never_reused_and_padding(con):
    core.add_student(con, "A")
    con.execute("UPDATE students SET code = 'S09' WHERE id = 1")
    con.commit()
    assert core.add_student(con, "B")["code"] == "S10"
    core.update_student(con, "S10", active=False)
    assert core.add_student(con, "C")["code"] == "S11"
    con.execute("UPDATE students SET code = 'S120' WHERE code = 'S11'")
    con.commit()
    assert core.add_student(con, "D")["code"] == "S121"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "x" * 101},
        {"name": "A", "weekly_goal": 0},
        {"name": "A", "weekly_goal": 51},
        {"name": "A", "weekly_goal": "abc"},
        {"name": "A", "weekly_goal": True},
        {"name": "A", "weekly_goal": 2.5},
        {"name": "A", "tiktok_url": YT},
        {"name": "A", "youtube_url": TT},
        {"name": "A", "tiktok_url": "https://www.tiktok.com/@lan/video/7300000000000000001"},
        {"name": "A", "youtube_url": "http://www.youtube.com/@lan"},
    ],
)
def test_add_student_validation(con, kwargs):
    with pytest.raises(ValueError):
        core.add_student(con, **kwargs)


def test_get_student_by_id_and_code(con):
    s = core.add_student(con, "Lan")
    assert core.get_student(con, s["id"])["code"] == "S01"
    assert core.get_student(con, "S01")["id"] == s["id"]
    assert core.get_student(con, "s01")["id"] == s["id"]
    assert core.get_student(con, str(s["id"]))["id"] == s["id"]
    for ref in (99, "S99", "", None, True):
        with pytest.raises(ValueError, match="Không tìm thấy"):
            core.get_student(con, ref)


def test_update_student(con):
    s = core.add_student(con, "Lan")
    updated = core.update_student(
        con, s["id"], name="Lan Anh", weekly_goal=4, tiktok_url="https://tiktok.com/@lan",
        youtube_url="https://www.youtube.com/@lan/",
    )
    assert updated["name"] == "Lan Anh" and updated["weekly_goal"] == 4
    assert updated["tiktok_url"] == TT and updated["youtube_url"] == YT
    cleared = core.update_student(con, "S01", tiktok_url="", active=False)
    assert cleared["tiktok_url"] == "" and cleared["active"] is False
    assert core.update_student(con, "S01") == cleared  # no-op
    with pytest.raises(ValueError, match="Trường không hợp lệ"):
        core.update_student(con, "S01", code="S05")
    with pytest.raises(ValueError):
        core.update_student(con, "S01", weekly_goal=0)
    with pytest.raises(ValueError):
        core.update_student(con, "S01", youtube_url=TT)
    with pytest.raises(ValueError):
        core.update_student(con, "S01", name="  ")
    with pytest.raises(ValueError):
        core.update_student(con, "S01", active="maybe")


def test_list_students_inactive(con):
    core.add_student(con, "A")
    b = core.add_student(con, "B")
    core.update_student(con, b["id"], active=False)
    assert [s["name"] for s in core.list_students(con)] == ["A"]
    assert [s["name"] for s in core.list_students(con, include_inactive=True)] == ["A", "B"]


# ---------------------------------------------------------------------------
# Videos and sync runs
# ---------------------------------------------------------------------------

def test_record_video(con):
    s = core.add_student(con, "Lan")
    v = core.record_video(con, "S01", "https://youtu.be/abcdefghijk?t=3", "2026-11-17", " Hi ")
    assert v["platform"] == "youtube" and v["video_key"] == "abcdefghijk"
    assert v["url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert v["title"] == "Hi" and v["source"] == "manual" and v["student_id"] == s["id"]


def test_record_video_duplicates(con):
    core.add_student(con, "Lan")
    core.add_student(con, "Minh")
    core.record_video(con, 1, "https://youtu.be/abcdefghijk", "2026-11-17")
    with pytest.raises(ValueError, match="Liên kết này đã được ghi nhận."):
        core.record_video(con, 1, "https://www.youtube.com/shorts/abcdefghijk", "2026-11-17")
    with pytest.raises(ValueError, match="Liên kết này đã được ghi nhận."):
        core.record_video(con, 2, "https://www.youtube.com/watch?v=abcdefghijk", "2026-11-16")
    core.record_video(con, 1, "https://vm.tiktok.com/ZM123xyz/", "2026-11-17")
    with pytest.raises(ValueError, match="Liên kết này đã được ghi nhận."):
        core.record_video(con, 1, "https://vm.tiktok.com/ZM123xyz?x=1", "2026-11-17")


@pytest.mark.parametrize(
    "date", ["2026-11-19", "2026/11/17", "20261117", "2026-02-30", "", None, "hôm qua"]
)
def test_record_video_rejects_bad_dates(con, date):
    core.add_student(con, "Lan")
    with pytest.raises(ValueError):
        core.record_video(con, 1, tiktok_url(1), date)


def test_record_video_today_ok_and_other_errors(con):
    core.add_student(con, "Lan")
    assert core.record_video(con, 1, tiktok_url(1), "2026-11-18")["published_date"] == "2026-11-18"
    with pytest.raises(ValueError):
        core.record_video(con, 1, "https://example.com/v", "2026-11-18")
    with pytest.raises(ValueError):
        core.record_video(con, 1, tiktok_url(2), "2026-11-18", source="bogus")
    with pytest.raises(ValueError, match="Không tìm thấy"):
        core.record_video(con, 42, tiktok_url(3), "2026-11-18")


def test_upsert_observed_videos(con):
    core.add_student(con, "Lan", tiktok_url=TT)
    core.record_video(con, 1, tiktok_url(1), "2026-11-16")
    videos = [
        {"video_key": str(7300000000000000000 + n), "url": tiktok_url(n), "title": "t",
         "published_date": "2026-11-17"}
        for n in (1, 2, 3)
    ]
    assert core.upsert_observed_videos(con, 1, "tiktok", videos, "observed") == {
        "added": 2, "foreign": []}
    assert core.upsert_observed_videos(con, 1, "tiktok", videos, "observed")["added"] == 0
    # Existing manual row keeps its source.
    row = con.execute("SELECT source FROM videos WHERE video_key = ?",
                      (str(7300000000000000001),)).fetchone()
    assert row["source"] == "manual"
    with pytest.raises(ValueError):
        core.upsert_observed_videos(con, 1, "vimeo", videos, "observed")
    with pytest.raises(ValueError):
        core.upsert_observed_videos(con, 1, "tiktok", videos, "guess")
    with pytest.raises(ValueError):
        core.upsert_observed_videos(con, 1, "tiktok", [{"video_key": "", "url": "x",
                                                         "published_date": "2026-11-17"}], "api")


def test_record_sync_run_normalises(con):
    s = core.add_student(con, "Lan", tiktok_url=TT)
    run_id = core.record_sync_run(
        con, s["id"], "tiktok", "ingest:mac", "ok", "2026-11-18T03:00:00Z",
        "2026-11-18T03:05:00+00:00", 30, 2, ["2026-11-17", dt.date(2026, 11, 16)], "",
    )
    row = con.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["started_at"] == "2026-11-18T10:00:00+07:00"
    assert row["finished_at"] == "2026-11-18T10:05:00+07:00"
    assert json.loads(row["dates_json"]) == ["2026-11-17", "2026-11-16"]
    for bad in ({"status": "done"}, {"platform": "x"}, {"started_at": "yesterday"},
                {"collector": ""}, {"fetch_limit": -1}):
        args = dict(platform="tiktok", collector="yt-dlp", status="ok",
                    started_at=NOW, finished_at=NOW, fetch_limit=30, fetched_count=0, dates=[])
        args.update(bad)
        with pytest.raises(ValueError):
            core.record_sync_run(con, s["id"], **args)


# ---------------------------------------------------------------------------
# Week bounds
# ---------------------------------------------------------------------------

def test_week_bounds(freeze_time):
    assert core.week_bounds() == (dt.date(2026, 11, 16), dt.date(2026, 11, 22))
    assert core.week_bounds(-1) == (dt.date(2026, 11, 9), dt.date(2026, 11, 15))
    freeze_time(vn(2026, 11, 16, 0, 0))  # Monday midnight
    assert core.week_bounds() == (dt.date(2026, 11, 16), dt.date(2026, 11, 22))
    freeze_time(vn(2026, 11, 22, 23, 59))  # Sunday night
    assert core.week_bounds() == (dt.date(2026, 11, 16), dt.date(2026, 11, 22))
    # Sunday 18:00 UTC is already Monday in VN.
    freeze_time(dt.datetime(2026, 11, 22, 18, 0, tzinfo=dt.timezone.utc))
    assert core.week_bounds() == (dt.date(2026, 11, 23), dt.date(2026, 11, 29))


def test_week_progress_rejects_future(con, settings):
    with pytest.raises(ValueError):
        core.week_progress(con, 1, settings)


# ---------------------------------------------------------------------------
# Status rules
# ---------------------------------------------------------------------------

def test_never_synced(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT)
    e = progress_of(con, settings, s)
    assert e["status"] == "insufficient_data"
    assert e["status_label"] == "Chưa đủ dữ liệu để kết luận"
    assert e["platforms"]["tiktok"]["coverage"] == "unknown"
    assert e["platforms"]["tiktok"]["reason"] == "never_synced"
    assert e["platforms"]["tiktok"]["last_status"] == ""
    assert e["reasons"] == ["tiktok:never_synced"]
    assert "youtube" not in e["platforms"]


def test_last_sync_failed(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT)
    run(con, s, status="failed", fetched=0, error="Bị giới hạn tần suất")
    e = progress_of(con, settings, s)
    p = e["platforms"]["tiktok"]
    assert e["status"] == "insufficient_data"
    assert (p["coverage"], p["reason"]) == ("unknown", "last_sync_failed")
    assert p["last_status"] == "failed" and p["last_error"] == "Bị giới hạn tần suất"
    assert p["last_synced_at"] == ""


def test_stale_sync_for_current_week(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT)
    run(con, s, finished=NOW - dt.timedelta(hours=37), fetched=2)
    e = progress_of(con, settings, s)
    assert e["platforms"]["tiktok"]["reason"] == "stale"
    assert e["status"] == "insufficient_data"
    # Within FRESH_HOURS it is fine.
    run(con, s, finished=NOW - dt.timedelta(hours=35), fetched=2)
    assert progress_of(con, settings, s)["status"] == "below_goal"


def test_fresh_hours_from_settings(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT)
    run(con, s, finished=NOW - dt.timedelta(hours=10), fetched=2)
    settings.fresh_hours = 6
    assert progress_of(con, settings, s)["platforms"]["tiktok"]["reason"] == "stale"


def test_failed_latest_but_fresh_older_success(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT)
    ok_id = run(con, s, finished=NOW - dt.timedelta(hours=5), fetched=3)
    run(con, s, status="failed", finished=NOW - dt.timedelta(hours=1), fetched=0, error="Lỗi")
    e = progress_of(con, settings, s)
    p = e["platforms"]["tiktok"]
    assert e["status"] == "insufficient_data"
    assert p["coverage"] == "unknown" and p["reason"] == "last_sync_failed"
    assert p["last_status"] == "failed" and p["last_error"] == "Lỗi"
    ok_row = con.execute("SELECT finished_at FROM sync_runs WHERE id = ?", (ok_id,)).fetchone()
    assert p["last_synced_at"] == ok_row["finished_at"]


def test_failed_latest_and_old_success_is_failed(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT)
    run(con, s, finished=NOW - dt.timedelta(hours=50), fetched=3)
    run(con, s, status="failed", finished=NOW - dt.timedelta(hours=1), fetched=0)
    p = progress_of(con, settings, s)["platforms"]["tiktok"]
    assert (p["coverage"], p["reason"]) == ("unknown", "last_sync_failed")


def test_complete_when_channel_read_fully(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT)
    run(con, s, limit=30, fetched=29, dates=["2026-11-17"] * 29)
    e = progress_of(con, settings, s)
    assert e["platforms"]["tiktok"]["coverage"] == "complete"
    assert e["status"] == "below_goal"
    assert e["status_label"] == "Thiếu theo dữ liệu hiện có"
    assert e["reasons"] == []


def test_tiktok_guard_needs_more_than_three_older(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT)
    three_old = ["2026-11-17"] * 27 + ["2026-11-15", "2026-10-01", "2026-09-01"]
    run(con, s, limit=30, fetched=30, dates=three_old)
    e = progress_of(con, settings, s)
    assert e["platforms"]["tiktok"]["coverage"] == "lower_bound"
    assert e["status"] == "at_least"
    assert e["status_label"].startswith("Ít nhất 0 video")
    assert e["reasons"] == ["tiktok:lower_bound"]
    four_old = ["2026-11-17"] * 26 + ["2026-11-15", "2026-11-14", "2026-10-01", "2026-09-01"]
    run(con, s, limit=30, fetched=30, dates=four_old)
    assert progress_of(con, settings, s)["platforms"]["tiktok"]["coverage"] == "complete"


def test_youtube_guard_zero(con, settings):
    s = core.add_student(con, "Lan", 3, youtube_url=YT)
    run(con, s, "youtube", limit=30, fetched=30, dates=["2026-11-17"] * 30)
    assert progress_of(con, settings, s)["platforms"]["youtube"]["coverage"] == "lower_bound"
    run(con, s, "youtube", limit=30, fetched=30, dates=["2026-11-17"] * 29 + ["2026-11-15"])
    assert progress_of(con, settings, s)["platforms"]["youtube"]["coverage"] == "complete"


def test_partial_run_caps_at_lower_bound(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT)
    run(con, s, status="partial", limit=30, fetched=4, dates=["2026-11-01"] * 4)
    e = progress_of(con, settings, s)
    assert e["platforms"]["tiktok"]["coverage"] == "lower_bound"
    assert e["status"] == "at_least"


def test_unknown_coverage_overrides_met(con, settings):
    s = core.add_student(con, "Lan", 2, tiktok_url=TT, youtube_url=YT)
    core.record_video(con, s["id"], tiktok_url(1), "2026-11-16")
    core.record_video(con, s["id"], youtube_url(1), "2026-11-18")
    e = progress_of(con, settings, s)
    assert e["count"] == 2
    assert e["status"] == "insufficient_data"
    assert set(e["reasons"]) == {"tiktok:never_synced", "youtube:never_synced"}
    assert e["platforms"]["tiktok"]["coverage"] == "unknown"


def test_student_without_channel(con, settings):
    s = core.add_student(con, "Lan", 2)
    e = progress_of(con, settings, s)
    assert e["status"] == "insufficient_data"
    assert e["reasons"] == ["no_channel"]
    assert e["platforms"] == {}
    core.record_video(con, s["id"], tiktok_url(1), "2026-11-17")
    e = progress_of(con, settings, s)
    assert e["status"] == "insufficient_data" and e["count"] == 1
    core.record_video(con, s["id"], tiktok_url(2), "2026-11-17")
    assert progress_of(con, settings, s)["status"] == "insufficient_data"


def test_latest_failure_overrides_goal_then_success_restores_status(con, settings):
    s = core.add_student(con, "Lan", 1, tiktok_url=TT)
    core.record_video(con, s["id"], tiktok_url(1), "2026-11-17")
    run(con, s, finished=NOW - dt.timedelta(hours=2), fetched=1)
    assert progress_of(con, settings, s)["status"] == "met"
    run(con, s, status="failed", finished=NOW - dt.timedelta(hours=1), fetched=0)
    assert progress_of(con, settings, s)["status"] == "insufficient_data"
    run(con, s, finished=NOW, fetched=1)
    assert progress_of(con, settings, s)["status"] == "met"


def test_stale_coverage_overrides_goal(con, settings):
    s = core.add_student(con, "Lan", 1, tiktok_url=TT)
    core.record_video(con, s["id"], tiktok_url(1), "2026-11-17")
    run(con, s, finished=NOW - dt.timedelta(hours=37), fetched=1)
    e = progress_of(con, settings, s)
    assert e["count"] == 1 and e["status"] == "insufficient_data"
    assert e["reasons"] == ["tiktok:stale"]


def test_manual_and_observed_videos_count(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT)
    core.record_video(con, s["id"], youtube_url(1), "2026-11-17", source="reach")
    core.record_video(con, s["id"], tiktok_url(1), "2026-11-16")
    core.upsert_observed_videos(con, s["id"], "tiktok", [
        {"video_key": "7300000000000000002", "url": tiktok_url(2), "title": "",
         "published_date": "2026-11-18"},
    ], "observed")
    run(con, s, fetched=3, dates=["2026-11-18", "2026-11-16", "2026-11-01"])
    e = progress_of(con, settings, s)
    assert e["count"] == 3 and e["status"] == "met"
    assert e["by_source"] == {"manual": 1, "observed": 1, "api": 0, "reach": 1}
    assert [v["published_date"] for v in e["videos"]] == ["2026-11-16", "2026-11-17", "2026-11-18"]
    assert set(e["videos"][0]) == {"platform", "url", "title", "published_date", "source", "countable"}
    assert e["unreadable_posts"] == 0 and all(v["countable"] is True for v in e["videos"])


def test_inactive_students_excluded(con, settings):
    a = core.add_student(con, "A", tiktok_url=TT)
    b = core.add_student(con, "B")
    core.update_student(con, b["id"], active=False)
    progress = core.week_progress(con, 0, settings)
    assert [s["id"] for s in progress["students"]] == [a["id"]]
    assert progress["summary"]["students"] == 1


def test_week_boundaries_sunday(con, settings, freeze_time):
    freeze_time(vn(2026, 11, 22, 23, 0))  # Sunday night
    s = core.add_student(con, "Lan", 3)
    core.record_video(con, s["id"], tiktok_url(1), "2026-11-22")  # this Sunday: counted
    core.record_video(con, s["id"], tiktok_url(2), "2026-11-16")  # Monday: counted
    core.record_video(con, s["id"], tiktok_url(3), "2026-11-15")  # previous Sunday: not
    assert progress_of(con, settings, s)["count"] == 2
    assert progress_of(con, settings, s, -1)["count"] == 1


def test_week_boundaries_monday_morning(con, settings, freeze_time):
    freeze_time(vn(2026, 11, 16, 0, 30))
    s = core.add_student(con, "Lan", 1)
    core.record_video(con, s["id"], tiktok_url(1), "2026-11-15")
    progress = core.week_progress(con, 0, settings)
    assert progress["week_start"] == "2026-11-16"
    assert entry(progress, s)["count"] == 0
    previous = entry(core.week_progress(con, -1, settings), s)
    assert previous["count"] == 1 and previous["status"] == "insufficient_data"


def test_past_week_needed_after_next_monday(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT)
    # Last week is Mon 11-09 .. Sun 11-15; a run during that week is not enough.
    run(con, s, finished=vn(2026, 11, 15, 23, 59), fetched=3, dates=["2026-11-10"])
    e = progress_of(con, settings, s, -1)
    assert e["platforms"]["tiktok"]["reason"] == "stale"
    assert e["status"] == "insufficient_data"
    # A run at Monday 00:00 VN or later covers it, even if older than FRESH_HOURS.
    run(con, s, finished=vn(2026, 11, 16, 0, 0), fetched=3,
        dates=["2026-11-16", "2026-11-10", "2026-11-01"])
    e = progress_of(con, settings, s, -1)
    assert e["platforms"]["tiktok"]["coverage"] == "complete"
    assert e["status"] == "below_goal"
    # Same run is stale for the current week (Wed 12:00 minus 36h = Mon 24:00).
    assert progress_of(con, settings, s)["platforms"]["tiktok"]["reason"] == "stale"


def test_past_week_older_counts_relative_to_that_monday(con, settings):
    s = core.add_student(con, "Lan", 3, youtube_url=YT)
    run(con, s, "youtube", limit=3, fetched=3, dates=["2026-11-17", "2026-11-10", "2026-11-08"])
    assert progress_of(con, settings, s, -1)["platforms"]["youtube"]["coverage"] == "complete"
    run(con, s, "youtube", limit=3, fetched=3, dates=["2026-11-17", "2026-11-16", "2026-11-10"])
    assert progress_of(con, settings, s, -1)["platforms"]["youtube"]["coverage"] == "lower_bound"


def test_both_platforms_one_unknown(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT, youtube_url=YT)
    run(con, s, "youtube", fetched=2, dates=["2026-11-17", "2026-11-01"])
    e = progress_of(con, settings, s)
    assert e["platforms"]["youtube"]["coverage"] == "complete"
    assert e["platforms"]["tiktok"]["coverage"] == "unknown"
    assert e["status"] == "insufficient_data"
    assert e["reasons"] == ["tiktok:never_synced"]


def test_both_platforms_lower_bound_and_complete(con, settings):
    s = core.add_student(con, "Lan", 3, tiktok_url=TT, youtube_url=YT)
    run(con, s, "youtube", fetched=2, dates=["2026-11-17", "2026-11-01"])
    run(con, s, "tiktok", limit=2, fetched=2, dates=["2026-11-17", "2026-11-16"])
    e = progress_of(con, settings, s)
    assert e["status"] == "at_least"


def test_future_video_rows_not_counted(con, settings):
    s = core.add_student(con, "Lan", 1)
    core.upsert_observed_videos(con, s["id"], "tiktok", [
        {"video_key": "7300000000000000009", "url": tiktok_url(9), "title": "",
         "published_date": "2026-11-20"},
    ], "observed")
    assert progress_of(con, settings, s)["count"] == 0


def test_week_progress_shape(con, settings):
    a = core.add_student(con, "A", 1)
    core.record_video(con, a["id"], tiktok_url(1), "2026-11-17")
    core.add_student(con, "B", 1)
    b2 = core.add_student(con, "C", 1, tiktok_url=TT)
    run(con, b2, fetched=1, dates=["2026-11-01"])
    progress = core.week_progress(con, 0, settings)
    assert progress["week_start"] == "2026-11-16" and progress["week_end"] == "2026-11-22"
    assert progress["is_current"] is True
    assert progress["generated_at"] == "2026-11-18T12:00:00+07:00"
    assert progress["timezone"] == "Asia/Ho_Chi_Minh"
    assert progress["summary"] == {
        "students": 3, "met": 0, "at_least": 0, "below_goal": 1, "insufficient_data": 2,
    }
    assert progress["disclaimer"] == core.DISCLAIMER
    keys = {"id", "code", "name", "weekly_goal", "count", "status", "status_label", "reasons",
            "by_source", "platforms", "videos", "unreadable_posts"}
    assert all(set(s) == keys for s in progress["students"])
    platform = progress["students"][2]["platforms"]["tiktok"]
    assert set(platform) == {"channel_url", "coverage", "reason", "last_synced_at",
                             "last_status", "last_error", "segments"}
    json.dumps(progress)
    assert core.week_progress(con, -2, settings)["is_current"] is False


def test_settings_default_loaded_from_env(con, monkeypatch):
    monkeypatch.setenv("TRACKER_FRESH_HOURS", "2")
    s = core.add_student(con, "Lan", 3, tiktok_url=TT)
    run(con, s, finished=NOW - dt.timedelta(hours=3), fetched=1)
    assert entry(core.week_progress(con), s)["platforms"]["tiktok"]["reason"] == "stale"


# ---------------------------------------------------------------------------
# History and sync status
# ---------------------------------------------------------------------------

def test_student_history(con, settings):
    s = core.add_student(con, "Lan", 1, tiktok_url=TT)
    core.record_video(con, s["id"], tiktok_url(1), "2026-11-17")
    core.record_video(con, s["id"], tiktok_url(2), "2026-11-03")
    history = core.student_history(con, "S01", 3, settings)
    assert history["student"]["code"] == "S01"
    weeks = history["weeks"]
    assert [w["week_start"] for w in weeks] == ["2026-11-16", "2026-11-09", "2026-11-02"]
    assert [w["week_end"] for w in weeks] == ["2026-11-22", "2026-11-15", "2026-11-08"]
    assert [w["count"] for w in weeks] == [1, 0, 1]
    assert [w["status"] for w in weeks] == ["insufficient_data"] * 3
    for bad in (0, 53, "x"):
        with pytest.raises(ValueError):
            core.student_history(con, "S01", bad, settings)


def test_student_history_inactive_student(con, settings):
    s = core.add_student(con, "Lan", 1)
    core.update_student(con, s["id"], active=False)
    assert len(core.student_history(con, s["id"], 2, settings)["weeks"]) == 2


def test_sync_status(con):
    a = core.add_student(con, "A", tiktok_url=TT, youtube_url=YT)
    core.add_student(con, "B")
    c = core.add_student(con, "C", tiktok_url=TT)
    core.update_student(con, c["id"], active=False)
    run(con, a, "tiktok", finished=NOW - dt.timedelta(hours=5), fetched=4)
    run(con, a, "tiktok", status="failed", fetched=0, error="Lỗi")
    rows = core.sync_status(con)
    assert [(r["code"], r["platform"]) for r in rows] == [("S01", "tiktok"), ("S01", "youtube")]
    tiktok, youtube = rows
    assert tiktok["status"] == "failed" and tiktok["error"] == "Lỗi" and tiktok["synced"]
    assert tiktok["last_success_at"] == (NOW - dt.timedelta(hours=5)).isoformat(timespec="seconds")
    assert youtube["synced"] is False and youtube["status"] == ""


def test_now_is_read_at_call_time(con, settings, freeze_time):
    freeze_time(vn(2026, 12, 2, 9, 0))
    assert core.week_progress(con, 0, settings)["week_start"] == "2026-11-30"
    assert config.now_vn() == vn(2026, 12, 2, 9, 0)


# ---------------------------------------------------------------------------
# Unreadable posts (countable=0)
# ---------------------------------------------------------------------------

def _tt(n: int) -> dict:
    return {"video_key": str(7300000000000000000 + n), "url": tiktok_url(n), "title": f"v{n}",
            "published_date": "2026-11-17"}


def _photo(n: int, day="2026-11-17") -> dict:
    return {**_tt(n), "title": "", "published_date": day, "countable": False}


def test_unreadable_posts_not_counted(con, settings):
    s = core.add_student(con, "Lan", 2, tiktok_url=TT)
    added = core.upsert_observed_videos(con, s["id"], "tiktok", [_tt(1), _photo(2), _photo(3)],
                                        "observed")
    assert added == {"added": 3, "foreign": []}
    run(con, s, fetched=3, dates=["2026-11-17"] * 3)
    e = progress_of(con, settings, s)
    assert e["count"] == 1 and e["unreadable_posts"] == 2
    assert e["by_source"] == {"manual": 0, "observed": 1, "api": 0, "reach": 0}
    assert e["status"] == "below_goal"  # 1 < 2 even though 3 posts were seen
    assert [v["countable"] for v in e["videos"]] == [True, False, False]
    # Outside the week they are neither counted nor listed.
    last = progress_of(con, settings, s, -1)
    assert last["unreadable_posts"] == 0 and last["videos"] == []


def test_unreadable_post_does_not_meet_goal(con, settings):
    s = core.add_student(con, "Lan", 1, tiktok_url=TT)
    core.upsert_observed_videos(con, s["id"], "tiktok", [_photo(1)], "observed")
    run(con, s, fetched=1, dates=["2026-11-17"])
    e = progress_of(con, settings, s)
    assert (e["count"], e["unreadable_posts"], e["status"]) == (0, 1, "below_goal")


def test_countable_value_validated(con):
    s = core.add_student(con, "Lan", 2, tiktok_url=TT)
    with pytest.raises(ValueError):
        core.upsert_observed_videos(con, s["id"], "tiktok", [{**_tt(1), "countable": "no"}],
                                    "observed")


def test_later_sync_upgrades_unreadable_row(con, settings):
    s = core.add_student(con, "Lan", 1, tiktok_url=TT)
    assert core.upsert_observed_videos(con, s["id"], "tiktok", [_photo(1)], "observed")["added"] == 1
    # Same key read again as unreadable: nothing changes.
    assert core.upsert_observed_videos(con, s["id"], "tiktok", [_photo(1)], "observed")["added"] == 0
    # Now read successfully as a video: upgraded, not counted as added.
    assert core.upsert_observed_videos(con, s["id"], "tiktok", [_tt(1)], "observed")["added"] == 0
    row = con.execute("SELECT countable, title FROM videos").fetchone()
    assert (row["countable"], row["title"]) == (1, "v1")
    # A later unreadable report never downgrades it.
    core.upsert_observed_videos(con, s["id"], "tiktok", [_photo(1)], "observed")
    assert con.execute("SELECT countable FROM videos").fetchone()["countable"] == 1
    run(con, s, fetched=1, dates=["2026-11-17"])
    e = progress_of(con, settings, s)
    assert (e["count"], e["unreadable_posts"], e["status"]) == (1, 0, "met")


def test_upgrade_ignores_other_students_row(con):
    a = core.add_student(con, "Lan", 1, tiktok_url=TT)
    b = core.add_student(con, "Minh", 1)
    core.upsert_observed_videos(con, a["id"], "tiktok", [_photo(1)], "observed")
    # Another student's key is reported as foreign, never re-assigned or upgraded.
    assert core.upsert_observed_videos(con, b["id"], "tiktok", [_tt(1)], "observed") == {
        "added": 0, "foreign": [_tt(1)["video_key"]]}
    row = con.execute("SELECT student_id, countable FROM videos").fetchone()
    assert (row["student_id"], row["countable"]) == (a["id"], 0)


def test_record_video_upgrades_unreadable_row_same_student(con, settings):
    s = core.add_student(con, "Lan", 1, tiktok_url=TT)
    core.upsert_observed_videos(con, s["id"], "tiktok", [_photo(1, "2026-11-16")], "observed")
    video = core.record_video(con, s["id"], tiktok_url(1), "2026-11-17", title="Bài ảnh")
    assert video["countable"] is True and video["source"] == "manual"
    assert video["title"] == "Bài ảnh" and video["published_date"] == "2026-11-17"
    assert con.execute("SELECT count(*) FROM videos").fetchone()[0] == 1
    e = progress_of(con, settings, s)
    assert (e["count"], e["unreadable_posts"], e["by_source"]["manual"]) == (1, 0, 1)
    # Now it is a countable video: a second record is a duplicate.
    with pytest.raises(ValueError, match="đã được ghi nhận"):
        core.record_video(con, s["id"], tiktok_url(1), "2026-11-17")


def test_record_video_unreadable_row_of_other_student_is_duplicate(con):
    a = core.add_student(con, "Lan", 1, tiktok_url=TT)
    b = core.add_student(con, "Minh", 1)
    core.upsert_observed_videos(con, a["id"], "tiktok", [_photo(1)], "observed")
    with pytest.raises(ValueError, match="đã được ghi nhận"):
        core.record_video(con, b["id"], tiktok_url(1), "2026-11-17")


def test_disclaimer_has_no_forbidden_phrase():
    assert core.DISCLAIMER == (
        "Số liệu dựa trên dữ liệu thu thập được; kênh do giáo viên khai báo, chưa xác minh chủ sở "
        "hữu. Khi dữ liệu chưa đủ thì không kết luận về việc đăng video của học viên."
    )
    assert "chưa đăng" not in core.DISCLAIMER.lower()
    assert "không đăng" not in core.DISCLAIMER.lower()
