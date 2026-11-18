"""Collector tests: no network, no real yt-dlp (subprocess and HTTP are faked)."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from video_tracker import collect, config, core
from video_tracker.collect import _Proc
from conftest import vn

TT = "https://www.tiktok.com/@lan"
YT = "https://www.youtube.com/@lan"
KEY = "AIzaSyFAKE-KEY-1234567890"


def epoch(y, m, d, hh=0, mm=0):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc).timestamp())


def tt_line(vid="7000000000000000001", ts=None, **extra):
    data = {"id": vid, "title": f"t{vid[-3:]}", "timestamp": ts if ts is not None else epoch(2026, 11, 16, 3)}
    data.update(extra)
    return json.dumps(data)


def yt_line(vid="abcdefghijk", ts=None, **extra):
    data = {"id": vid, "title": f"y{vid}", "timestamp": ts if ts is not None else epoch(2026, 11, 16, 3)}
    data.update(extra)
    return json.dumps(data)


@pytest.fixture(autouse=True)
def now(freeze_time):
    # Tuesday 2026-11-17 12:00 VN
    return freeze_time(vn(2026, 11, 17, 12))


@pytest.fixture
def ytdlp(monkeypatch):
    """Install a fake yt-dlp. Returns (set_responses, calls)."""
    calls: list[tuple[list[str], float]] = []
    responses: dict = {}

    def fake_run(argv, timeout):
        calls.append((argv, timeout))
        handler = responses.get(argv[-1], responses.get("*"))
        if handler is None:
            return _Proc(1, "", "ERROR: no handler")
        return handler(argv) if callable(handler) else handler

    monkeypatch.setattr(collect, "_run", fake_run)
    monkeypatch.setattr(collect, "_ytdlp_argv", lambda: ["/fake/yt-dlp"])
    return responses, calls


# ---------------------------------------------------------------------------
# yt-dlp parsing
# ---------------------------------------------------------------------------

def test_tiktok_jsonl_ok_and_argv(ytdlp, settings):
    responses, calls = ytdlp
    responses[TT] = _Proc(0, tt_line("7000000000000000001") + "\n" + tt_line("7000000000000000002") + "\n")
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert (res.status, res.collector, res.fetched_count, res.fetch_limit) == ("ok", "yt-dlp", 2, 5)
    assert res.videos[0]["url"] == "https://www.tiktok.com/@lan/video/7000000000000000001"
    assert res.videos[0]["video_key"] == "7000000000000000001"
    argv, timeout = calls[0]
    assert argv == ["/fake/yt-dlp", "--ignore-config", "--no-warnings", "--skip-download", "--dump-json",
                    "--playlist-end", "5", "--socket-timeout", "15", "--", TT]
    assert timeout <= 180
    assert res.started_at and res.finished_at


def test_timestamp_to_vn_date_crosses_day_boundary(ytdlp, settings):
    responses, _ = ytdlp
    # 18:30Z on the 15th is 01:30 on the 16th in Vietnam.
    responses[TT] = _Proc(0, tt_line(ts=epoch(2026, 11, 15, 18, 30)) + "\n")
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.videos[0]["published_date"] == "2026-11-16"
    # 16:59Z is still 23:59 on the 15th.
    responses[TT] = _Proc(0, tt_line(ts=epoch(2026, 11, 15, 16, 59)) + "\n")
    assert collect.collect_channel("tiktok", TT, 5, settings).videos[0]["published_date"] == "2026-11-15"


def test_upload_date_fallback(ytdlp, settings):
    responses, _ = ytdlp
    line = json.dumps({"id": "7000000000000000003", "title": "x", "upload_date": "20261114"})
    responses[TT] = _Proc(0, line + "\n")
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "ok" and res.videos[0]["published_date"] == "2026-11-14"


def test_missing_date_is_partial_and_skipped(ytdlp, settings):
    responses, _ = ytdlp
    # id >> 32 is in 1977 (implausible), so the id cannot date it either.
    nodate = json.dumps({"id": "1000000000000000009", "title": "no date"})
    responses[TT] = _Proc(0, tt_line("7000000000000000001") + "\n" + nodate + "\n")
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "partial"
    assert [v["video_key"] for v in res.videos] == ["7000000000000000001"]
    assert res.fetched_count == 1
    assert res.error


def test_future_dated_video_skipped(ytdlp, settings):
    responses, _ = ytdlp
    future = tt_line("7000000000000000005", ts=epoch(2026, 11, 20))
    responses[TT] = _Proc(0, tt_line("7000000000000000001") + "\n" + future + "\n")
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert [v["video_key"] for v in res.videos] == ["7000000000000000001"]


def test_nonzero_returncode_with_output_is_partial(ytdlp, settings):
    responses, _ = ytdlp
    responses[TT] = _Proc(1, tt_line() + "\n", "ERROR: something broke")
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "partial" and len(res.videos) == 1 and res.error


def test_no_output_is_failed(ytdlp, settings):
    responses, _ = ytdlp
    responses[TT] = _Proc(1, "", "ERROR: boom")
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "failed" and res.videos == [] and res.fetched_count == 0 and res.error


def test_garbage_lines_ignored(ytdlp, settings):
    responses, _ = ytdlp
    responses[TT] = _Proc(0, "WARNING: hi\n{not json\n" + tt_line() + "\n")
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "ok" and len(res.videos) == 1


@pytest.mark.parametrize("stderr,expected", [
    ("ERROR: Sign in to confirm you're not a bot", "Nền tảng yêu cầu xác minh (chặn theo IP)"),
    ("ERROR: HTTP Error 429: Too Many Requests", "Bị giới hạn tần suất"),
])
def test_bot_and_rate_limit_mapping(ytdlp, settings, stderr, expected):
    responses, _ = ytdlp
    responses[TT] = _Proc(1, "", stderr)
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "failed" and res.error == expected


def test_timeout_is_failed(ytdlp, settings):
    responses, _ = ytdlp
    responses[TT] = _Proc(-1, "", "", timed_out=True)
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "failed" and "thời gian" in res.error


def test_ytdlp_missing(monkeypatch, settings):
    monkeypatch.setattr(collect, "_ytdlp_argv", lambda: None)
    called = []
    monkeypatch.setattr(collect, "_run", lambda *a, **k: called.append(1))
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "failed" and res.error == "Chưa cài yt-dlp" and not called


def test_ytdlp_argv_discovery(monkeypatch):
    monkeypatch.setattr(collect.shutil, "which", lambda name: "/opt/yt-dlp")
    assert collect._ytdlp_argv() == ["/opt/yt-dlp"]
    monkeypatch.setattr(collect.shutil, "which", lambda name: None)
    argv = collect._ytdlp_argv()
    assert argv is None or argv[1:] == ["-m", "yt_dlp"]


def test_bad_channel_url_fails_clearly(ytdlp, settings):
    res = collect.collect_channel("tiktok", "https://example.com/@x", 5, settings)
    assert res.status == "failed" and res.error


# ---------------------------------------------------------------------------
# YouTube two tabs
# ---------------------------------------------------------------------------

def test_youtube_two_tabs_merged_and_deduped(ytdlp, settings):
    responses, calls = ytdlp
    responses[f"{YT}/videos"] = _Proc(0, yt_line("aaaaaaaaaaa") + "\n" + yt_line("bbbbbbbbbbb") + "\n")
    responses[f"{YT}/shorts"] = _Proc(0, yt_line("bbbbbbbbbbb") + "\n" + yt_line("ccccccccccc") + "\n")
    res = collect.collect_channel("youtube", YT, 5, settings)
    assert res.status == "ok"
    assert sorted(v["video_key"] for v in res.videos) == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    assert res.videos[0]["url"].startswith("https://www.youtube.com/watch?v=")
    assert [c[0][-1] for c in calls] == [f"{YT}/videos", f"{YT}/shorts"]
    # fetched_count = busiest tab, not the merged total
    assert res.fetched_count == 2 and res.fetch_limit == 5


def test_youtube_fetched_count_hits_limit_when_one_tab_full(ytdlp, settings):
    responses, _ = ytdlp
    full = "\n".join(yt_line(f"vvvvvvvvvv{i}") for i in range(3)) + "\n"
    responses[f"{YT}/videos"] = _Proc(0, full)
    responses[f"{YT}/shorts"] = _Proc(0, yt_line("sssssssssss") + "\n")
    res = collect.collect_channel("youtube", YT, 3, settings)
    assert res.fetched_count == 3 == res.fetch_limit
    assert len(res.videos) == 4


def test_youtube_missing_tab_is_not_an_error(ytdlp, settings):
    responses, _ = ytdlp
    responses[f"{YT}/videos"] = _Proc(0, yt_line("aaaaaaaaaaa") + "\n")
    responses[f"{YT}/shorts"] = _Proc(1, "", "ERROR: [youtube:tab] lan: This channel does not have a shorts tab")
    res = collect.collect_channel("youtube", YT, 5, settings)
    assert res.status == "ok" and res.error == "" and len(res.videos) == 1


def test_youtube_other_tab_error_makes_partial(ytdlp, settings):
    responses, _ = ytdlp
    responses[f"{YT}/videos"] = _Proc(0, yt_line("aaaaaaaaaaa") + "\n")
    responses[f"{YT}/shorts"] = _Proc(1, "", "ERROR: HTTP Error 429: Too Many Requests")
    res = collect.collect_channel("youtube", YT, 5, settings)
    assert res.status == "partial" and "tần suất" in res.error


def test_youtube_both_tabs_fail(ytdlp, settings):
    responses, _ = ytdlp
    responses["*"] = _Proc(1, "", "ERROR: Sign in to confirm you're not a bot")
    res = collect.collect_channel("youtube", YT, 5, settings)
    assert res.status == "failed" and "xác minh" in res.error


# ---------------------------------------------------------------------------
# YouTube Data API
# ---------------------------------------------------------------------------

def api_client(handler, seen=None):
    def wrapped(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)
    return httpx.Client(transport=httpx.MockTransport(wrapped))


def good_handler(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    assert q["key"] == KEY
    if request.url.path.endswith("/channels"):
        assert q.get("forHandle") == "@lan" or q.get("id", "").startswith("UC")
        return httpx.Response(200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUlan"}}}]})
    assert request.url.path.endswith("/playlistItems")
    assert q["playlistId"] == "UUlan" and q["maxResults"] == "5"
    return httpx.Response(200, json={"items": [
        {"contentDetails": {"videoId": "aaaaaaaaaaa", "videoPublishedAt": "2026-11-15T18:30:00Z"},
         "snippet": {"title": "One"}},
        {"contentDetails": {"videoId": "bbbbbbbbbbb", "videoPublishedAt": "2026-11-12T01:00:00Z"},
         "snippet": {"title": "Two"}},
    ]})


def test_api_handle_path(settings, monkeypatch):
    monkeypatch.setattr(collect, "_run", lambda *a, **k: pytest.fail("yt-dlp must not run"))
    settings.youtube_api_key = KEY
    seen = []
    res = collect.collect_channel("youtube", YT, 5, settings, http_client=api_client(good_handler, seen))
    assert (res.status, res.collector, res.fetched_count) == ("ok", "youtube-api", 2)
    assert res.videos[0] == {"video_key": "aaaaaaaaaaa", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
                             "title": "One", "published_date": "2026-11-16"}
    assert len(seen) == 2


def test_api_channel_id_path(settings):
    settings.youtube_api_key = KEY
    url = "https://www.youtube.com/channel/UC" + "a" * 22
    res = collect.collect_channel("youtube", url, 5, settings, http_client=api_client(good_handler))
    assert res.status == "ok" and len(res.videos) == 2


def test_api_c_url_fails_clearly(settings):
    settings.youtube_api_key = KEY
    res = collect.collect_channel("youtube", "https://www.youtube.com/c/lan", 5, settings,
                                  http_client=api_client(good_handler))
    assert res.status == "failed" and "/c/" in res.error and KEY not in res.error


def test_api_key_never_in_error(settings):
    settings.youtube_api_key = KEY

    def boom(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    res = collect.collect_channel("youtube", YT, 5, settings, http_client=api_client(boom))
    assert res.status == "failed" and KEY not in res.error and "key=" not in res.error

    def forbidden(request):
        return httpx.Response(403, json={"error": {"message": f"bad key {KEY}"}})

    res = collect.collect_channel("youtube", YT, 5, settings, http_client=api_client(forbidden))
    assert res.status == "failed" and KEY not in res.error


def test_api_channel_not_found_and_missing_date(settings):
    settings.youtube_api_key = KEY
    res = collect.collect_channel("youtube", YT, 5, settings,
                                  http_client=api_client(lambda r: httpx.Response(200, json={})))
    assert res.status == "failed" and res.error

    def handler(request):
        if request.url.path.endswith("/channels"):
            return good_handler(request)
        return httpx.Response(200, json={"items": [
            {"contentDetails": {"videoId": "aaaaaaaaaaa", "videoPublishedAt": "2026-11-15T01:00:00Z"}},
            {"contentDetails": {"videoId": "bbbbbbbbbbb"}},
            {"contentDetails": {"videoId": "ccccccccccc", "videoPublishedAt": "2026-11-30T01:00:00Z"}},
        ]})

    res = collect.collect_channel("youtube", YT, 5, settings, http_client=api_client(handler))
    assert res.status == "partial"
    assert [v["video_key"] for v in res.videos] == ["aaaaaaaaaaa"]  # undated + future skipped


def test_error_cleaning_strips_query_and_key():
    text = collect._clean_error(f"fail https://x.test/a?key={KEY}&b=1 and {KEY}", (KEY,))
    assert KEY not in text and "?" not in text


# ---------------------------------------------------------------------------
# sync_student / sync_all with the real core + DB
# ---------------------------------------------------------------------------

def test_sync_student_idempotent(ytdlp, settings, con):
    responses, _ = ytdlp
    responses[TT] = _Proc(0, tt_line("7000000000000000001") + "\n" + tt_line("7000000000000000002") + "\n")
    student = core.add_student(con, "Lan", 3, tiktok_url=TT)
    first = collect.sync_student(con, student, settings)
    assert first == [{"platform": "tiktok", "status": "ok", "added": 2, "fetched_count": 2, "error": ""}]
    second = collect.sync_student(con, student, settings)
    assert second[0]["added"] == 0 and second[0]["status"] == "ok"
    assert con.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 2
    row = con.execute("SELECT source FROM videos").fetchone()
    assert row["source"] == "observed"


def test_sync_student_only_platforms_with_channel(ytdlp, settings, con):
    responses, calls = ytdlp
    responses[TT] = _Proc(0, tt_line() + "\n")
    student = core.add_student(con, "Lan", 3, tiktok_url=TT)
    out = collect.sync_student(con, student, settings)
    assert [r["platform"] for r in out] == ["tiktok"]
    assert collect.sync_student(con, student, settings, platforms=["youtube"]) == []


def test_failed_run_recorded_and_progress_insufficient(ytdlp, settings, con):
    responses, _ = ytdlp
    responses["*"] = _Proc(1, "", "ERROR: Sign in to confirm you're not a bot")
    student = core.add_student(con, "Lan", 3, tiktok_url=TT, youtube_url=YT)
    out = collect.sync_student(con, student, settings)
    assert [(r["platform"], r["status"], r["fetched_count"]) for r in out] == [
        ("tiktok", "failed", 0), ("youtube", "failed", 0)]
    runs = con.execute("SELECT status, fetched_count, dates_json FROM sync_runs").fetchall()
    assert len(runs) == 2 and all(r["status"] == "failed" and r["dates_json"] == "[]" for r in runs)
    progress = core.week_progress(con, 0, settings)
    s = progress["students"][0]
    assert s["status"] == "insufficient_data"
    assert s["platforms"]["tiktok"]["coverage"] == "unknown"
    assert s["platforms"]["tiktok"]["reason"] == "last_sync_failed"


def test_sync_then_progress_uses_vn_dates(ytdlp, settings, con):
    responses, _ = ytdlp
    responses[TT] = _Proc(0, tt_line("7000000000000000001", ts=epoch(2026, 11, 15, 18, 30)) + "\n")
    student = core.add_student(con, "Lan", 1, tiktok_url=TT)
    collect.sync_student(con, student, settings)
    s = core.week_progress(con, 0, settings)["students"][0]
    assert s["count"] == 1 and s["status"] == "met"  # 2026-11-16 is Monday in VN


def test_unexpected_exception_recorded_without_trace(monkeypatch, settings, con):
    def boom(*a, **k):
        raise RuntimeError(f"secret {KEY} Traceback")

    settings.youtube_api_key = KEY
    monkeypatch.setattr(collect, "collect_channel", boom)
    student = core.add_student(con, "Lan", 3, youtube_url=YT)
    out = collect.sync_student(con, student, settings)
    assert out[0]["status"] == "failed" and KEY not in out[0]["error"] and "Traceback" not in out[0]["error"]
    row = con.execute("SELECT status, error, collector FROM sync_runs").fetchone()
    assert row["status"] == "failed" and KEY not in row["error"] and row["error"]
    assert row["collector"] == "youtube-api"


def test_collect_channel_swallows_unexpected(monkeypatch, settings):
    monkeypatch.setattr(collect, "_collect_ytdlp", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "failed" and res.error


def test_api_source_stored_as_api(settings, con):
    settings.youtube_api_key = KEY
    settings.sync_limit = 5
    student = core.add_student(con, "Lan", 3, youtube_url=YT)
    out = collect.sync_student(con, student, settings, http_client=api_client(good_handler))
    assert out[0]["added"] == 2 and out[0]["status"] == "ok"
    assert {r["source"] for r in con.execute("SELECT source FROM videos")} == {"api"}
    assert con.execute("SELECT collector FROM sync_runs").fetchone()["collector"] == "youtube-api"


def test_sync_all_summary(ytdlp, settings, con):
    responses, _ = ytdlp
    responses[TT] = _Proc(0, tt_line() + "\n")
    responses[YT + "/videos"] = _Proc(1, "", "ERROR: HTTP Error 429")
    responses[YT + "/shorts"] = _Proc(1, "", "ERROR: HTTP Error 429")
    core.add_student(con, "Lan", 3, tiktok_url=TT)
    core.add_student(con, "Minh", 3, youtube_url=YT)
    core.add_student(con, "NoChannel", 3)
    summary = collect.sync_all(con, settings)
    assert summary["totals"] == {"runs": 2, "ok": 1, "partial": 0, "failed": 1, "added": 1}
    by_code = {s["code"]: s for s in summary["students"]}
    assert by_code["S01"]["name"] == "Lan"
    assert by_code["S01"]["results"][0]["status"] == "ok"
    assert by_code["S02"]["results"][0]["error"] == "Bị giới hạn tần suất"
    assert by_code["S03"]["results"] == []


# ---------------------------------------------------------------------------
# TikTok per-item errors (photo posts) and id-derived dates
# ---------------------------------------------------------------------------

def tt_id(y, m, d, hh=0, mm=0, ss=0, low=12345):
    """A TikTok post id whose upper 32 bits are the given UTC moment."""
    seconds = int(dt.datetime(y, m, d, hh, mm, ss, tzinfo=dt.timezone.utc).timestamp())
    return str((seconds << 32) | low)


def item_error(vid, tag="TikTok"):
    return f"ERROR: [{tag}] {vid}: No video formats found!; please report this issue"


def test_tiktok_id_date_real_ids_and_vn_boundary():
    # Verified against yt-dlp upload dates.
    assert collect._tiktok_id_date("7708408003065580831") == "2026-11-15"
    assert collect._tiktok_id_date("7689510970292489502") == "2026-09-25"
    # 2026-11-05T19:38:43Z is already 06/11 in Vietnam.
    assert collect._tiktok_id_date("7704774145829063967") == "2026-11-06"
    assert collect._tiktok_id_date(tt_id(2026, 11, 15, 16, 59, 59)) == "2026-11-15"
    assert collect._tiktok_id_date(tt_id(2026, 11, 15, 17, 0, 0)) == "2026-11-16"


def test_tiktok_id_date_rejects_implausible():
    assert collect._tiktok_id_date("1000000000000000009") is None  # 1977
    assert collect._tiktok_id_date(tt_id(2015, 12, 31, 23, 59, 59)) is None
    assert collect._tiktok_id_date(tt_id(2016, 1, 1)) == "2016-01-01"
    assert collect._tiktok_id_date(tt_id(2026, 11, 17, 6)) is None  # after now (05:00Z)
    assert collect._tiktok_id_date("123") is None
    assert collect._tiktok_id_date("abc") is None


def test_tiktok_item_error_becomes_unreadable_post_and_run_ok(ytdlp, settings):
    responses, _ = ytdlp
    photo = "7704774145829063967"
    stdout = tt_line("7000000000000000001") + "\n" + tt_line("7000000000000000002") + "\n"
    responses[TT] = _Proc(1, stdout, item_error(photo) + "\n")
    res = collect.collect_channel("tiktok", TT, 3, settings)
    assert res.status == "ok" and res.error == ""
    assert res.fetched_count == 3
    unreadable = [v for v in res.videos if v.get("countable") is False]
    assert unreadable == [{
        "video_key": photo, "url": f"https://www.tiktok.com/@lan/video/{photo}",
        "title": "", "published_date": "2026-11-06", "countable": False,
    }]
    assert sum(1 for v in res.videos if v.get("countable", True)) == 2


def test_tiktok_item_error_tag_case_insensitive(ytdlp, settings):
    responses, _ = ytdlp
    photo = tt_id(2026, 11, 16, 3)
    responses[TT] = _Proc(1, tt_line() + "\n", item_error(photo, tag="tiktok") + "\n")
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "ok" and [v["video_key"] for v in res.videos][-1] == photo


def test_tiktok_mixed_errors_stay_partial(ytdlp, settings):
    responses, _ = ytdlp
    photo = tt_id(2026, 11, 16, 3)
    stderr = item_error(photo) + "\nERROR: [TikTok] tiktok: Unable to extract secondary user info\n"
    responses[TT] = _Proc(1, tt_line() + "\n", stderr)
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "partial" and res.error
    # The datable photo post is still recorded as unreadable.
    assert any(v["video_key"] == photo and v["countable"] is False for v in res.videos)
    assert res.fetched_count == 2


def test_tiktok_undatable_item_error_stays_partial(ytdlp, settings):
    responses, _ = ytdlp
    responses[TT] = _Proc(1, tt_line() + "\n", item_error("1000000000000000009") + "\n")
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "partial"
    assert [v["video_key"] for v in res.videos] == ["7000000000000000001"]
    assert res.fetched_count == 1


def test_tiktok_nonzero_without_error_lines_stays_partial(ytdlp, settings):
    responses, _ = ytdlp
    responses[TT] = _Proc(1, tt_line() + "\n", "WARNING: something\n")
    assert collect.collect_channel("tiktok", TT, 5, settings).status == "partial"


def test_tiktok_item_error_for_parsed_video_not_duplicated(ytdlp, settings):
    responses, _ = ytdlp
    vid = "7000000000000000001"
    photo = tt_id(2026, 11, 16, 3)
    stderr = "\n".join([item_error(vid), item_error(photo), item_error(photo)]) + "\n"
    responses[TT] = _Proc(1, tt_line(vid) + "\n", stderr)
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "ok"
    assert [v["video_key"] for v in res.videos] == [vid, photo]
    assert res.videos[0].get("countable", True) is True


def test_tiktok_only_unreadable_posts_is_not_failed(ytdlp, settings):
    responses, _ = ytdlp
    photo = tt_id(2026, 11, 16, 3)
    responses[TT] = _Proc(1, "", item_error(photo) + "\n")
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "ok" and res.fetched_count == 1 and res.videos[0]["countable"] is False


def test_tiktok_missing_date_falls_back_to_id(ytdlp, settings):
    responses, _ = ytdlp
    vid = tt_id(2026, 11, 15, 18, 30)  # 01:30 on the 16th in VN
    responses[TT] = _Proc(0, json.dumps({"id": vid, "title": "no date"}) + "\n")
    res = collect.collect_channel("tiktok", TT, 5, settings)
    assert res.status == "ok" and res.fetched_count == 1
    assert res.videos == [{"video_key": vid, "url": f"https://www.tiktok.com/@lan/video/{vid}",
                           "title": "no date", "published_date": "2026-11-16",
                           "metrics": {"view_count": None, "like_count": None, "comment_count": None}}]


def test_youtube_errors_not_treated_as_tiktok_items(ytdlp, settings):
    responses, _ = ytdlp
    photo = tt_id(2026, 11, 16, 3)
    responses[YT + "/videos"] = _Proc(1, yt_line() + "\n", item_error(photo) + "\n")
    responses[YT + "/shorts"] = _Proc(0, "")
    res = collect.collect_channel("youtube", YT, 5, settings)
    assert res.status == "partial"
    assert [v["video_key"] for v in res.videos] == ["abcdefghijk"]
    # A YouTube entry with an id-like value but no date is still dropped.
    responses[YT + "/videos"] = _Proc(0, json.dumps({"id": "abcdefghijk"}) + "\n")
    assert collect.collect_channel("youtube", YT, 5, settings).status == "partial"


def test_sync_student_stores_unreadable_post(ytdlp, settings, con):
    responses, _ = ytdlp
    photo = tt_id(2026, 11, 16, 3)
    responses[TT] = _Proc(1, tt_line() + "\n", item_error(photo) + "\n")
    student = core.add_student(con, "Lan", 3, tiktok_url=TT)
    out = collect.sync_student(con, student, settings)
    assert out[0]["status"] == "ok" and out[0]["added"] == 2 and out[0]["fetched_count"] == 2
    rows = {r["video_key"]: r["countable"] for r in con.execute("SELECT * FROM videos")}
    assert rows == {"7000000000000000001": 1, photo: 0}
    run = con.execute("SELECT status, dates_json FROM sync_runs").fetchone()
    assert run["status"] == "ok" and sorted(json.loads(run["dates_json"])) == ["2026-11-16", "2026-11-16"]
