"""CLI tests: drive cli.main with a temporary DB; no network, no real yt-dlp."""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import vn
from video_tracker import cli, collect, config, db

TOKEN = "tok-secret-123"
TT = "https://www.tiktok.com/@alice"
YT = "https://www.youtube.com/@alice"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("TRACKER_") or name in ("YOUTUBE_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def dbp(tmp_path):
    return str(tmp_path / "t.sqlite3")


def run(dbp, capsys, *argv):
    code = cli.main(["--db", dbp, *argv])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def run_json(dbp, capsys, *argv):
    code, out, err = run(dbp, capsys, *argv, "--json")
    assert code == 0, err
    return json.loads(out)


def add(dbp, capsys, name="Lan", goal="2", *extra):
    return run_json(dbp, capsys, "add-student", name, "--goal", goal, *extra)


# ---------------------------------------------------------------------------

def test_add_list_update_deactivate_flow(dbp, capsys):
    code, out, _ = run(dbp, capsys, "add-student", "Lan", "--goal", "2", "--tiktok", TT)
    assert code == 0 and "S01" in out
    s = add(dbp, capsys, "Minh", "3", "--youtube", YT)
    assert s["code"] == "S02" and s["youtube_url"] == YT

    code, out, _ = run(dbp, capsys, "list")
    assert code == 0 and "S01" in out and "Lan" in out and "TikTok" in out and "YouTube" in out
    assert len(run_json(dbp, capsys, "list")["students"]) == 2

    u = run_json(dbp, capsys, "update", "S01", "--name", "Lan Anh", "--goal", "4", "--youtube", YT)
    assert (u["name"], u["weekly_goal"], u["youtube_url"]) == ("Lan Anh", 4, YT)
    u = run_json(dbp, capsys, "update", "S01", "--tiktok", "")
    assert u["tiktok_url"] == ""

    d = run_json(dbp, capsys, "deactivate", "S02")
    assert d["active"] == 0
    assert [s["code"] for s in run_json(dbp, capsys, "list")["students"]] == ["S01"]
    assert len(run_json(dbp, capsys, "list", "--all")["students"]) == 2
    u = run_json(dbp, capsys, "update", "S02", "--activate")
    assert u["active"] == 1
    code, out, _ = run(dbp, capsys, "deactivate", "S02")
    assert code == 0 and "S02" in out


def test_update_without_fields_and_unknown_ref(dbp, capsys):
    add(dbp, capsys)
    code, out, err = run(dbp, capsys, "update", "S01")
    assert code == 1 and out == "" and "cập nhật" in err
    code, _, err = run(dbp, capsys, "update", "S99", "--name", "X")
    assert code == 1 and "Không tìm thấy" in err


def test_errors_exit_1_with_vietnamese_stderr(dbp, capsys):
    code, out, err = run(dbp, capsys, "add-student", "X", "--goal", "99")
    assert code == 1 and out == "" and "Mục tiêu" in err
    code, _, err = run(dbp, capsys, "add-student", "X", "--tiktok", "http://evil.example/@x")
    assert code == 1 and err.strip()
    code, _, err = run(dbp, capsys, "record", "S01", "https://youtu.be/dQw4w9WgXcQ", "--date", "2026-11-01")
    assert code == 1 and "Không tìm thấy" in err


def test_argparse_error_returns_int(dbp, capsys):
    assert cli.main(["--db", dbp, "nope"]) == 2
    capsys.readouterr()


def test_unexpected_exception(dbp, capsys, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(cli.core, "list_students", boom)
    code, out, err = run(dbp, capsys, "list")
    assert code == 1 and "RuntimeError" in err and "secret detail" not in err
    assert "Traceback" not in err


def test_debug_env_reraises(dbp, capsys, monkeypatch):
    monkeypatch.setenv("TRACKER_DEBUG", "1")
    monkeypatch.setattr(cli.core, "list_students", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        cli.main(["--db", dbp, "list"])


def test_keyboard_interrupt(dbp, capsys, monkeypatch):
    def kb(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.core, "list_students", kb)
    assert cli.main(["--db", dbp, "list"]) == 130
    capsys.readouterr()


def test_record_and_progress(dbp, capsys, freeze_time):
    freeze_time(vn(2026, 11, 18))  # Wednesday
    add(dbp, capsys, "Lan", "1", "--tiktok", TT)
    add(dbp, capsys, "Minh", "3")
    v = run_json(dbp, capsys, "record", "S01", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                 "--date", "2026-11-17", "--title", "Bài 1")
    assert v["video"]["platform"] == "youtube" and v["student"]["code"] == "S01"
    # duplicate
    code, _, err = run(dbp, capsys, "record", "S01", "https://youtu.be/dQw4w9WgXcQ", "--date", "2026-11-17")
    assert code == 1 and "đã được ghi nhận" in err

    p = run_json(dbp, capsys, "progress")
    assert p["summary"]["met"] == 0 and p["summary"]["insufficient_data"] == 2
    code, out, _ = run(dbp, capsys, "progress")
    assert code == 0
    assert "S01 Lan 1/1" in out
    assert "Chưa đủ dữ liệu để kết luận (2)" in out and "S02 Minh 0/3" in out
    assert "2026-11-16" in out

    code, _, err = run(dbp, capsys, "progress", "--week-offset", "1")
    assert code == 1 and err.strip()

    h = run_json(dbp, capsys, "history", "S01", "--weeks", "2")
    assert len(h["weeks"]) == 2
    code, out, _ = run(dbp, capsys, "history", "S01", "--weeks", "2")
    assert code == 0 and "S01 Lan" in out and "1/1" in out


def _fake_collect(monkeypatch, status="ok", error=""):
    def fake(platform, channel_url, limit, settings, http_client=None):
        vids = [] if status == "failed" else [
            {"video_key": "a" * 11, "url": "https://www.youtube.com/watch?v=" + "a" * 11,
             "title": "t", "published_date": "2026-11-17"}]
        return collect.CollectResult(
            platform=platform, collector="yt-dlp", status=status, fetch_limit=limit,
            fetched_count=len(vids), videos=vids, error=error,
            started_at=vn(2026, 11, 18), finished_at=vn(2026, 11, 18))

    monkeypatch.setattr(collect, "collect_channel", fake)


def test_sync_text_and_json(dbp, capsys, monkeypatch, freeze_time):
    freeze_time(vn(2026, 11, 18))
    _fake_collect(monkeypatch)
    add(dbp, capsys, "Lan", "1", "--youtube", YT)
    add(dbp, capsys, "Minh", "1")
    code, out, _ = run(dbp, capsys, "sync", "S01", "--platform", "youtube")
    assert code == 0 and "S01 Lan · YouTube: ok" in out and "thêm mới 1" in out
    data = run_json(dbp, capsys, "sync")
    assert data["totals"]["runs"] == 1 and data["students"][0]["results"][0]["added"] == 0
    code, out, _ = run(dbp, capsys, "sync")
    assert "S02 Minh: (không có kênh" in out

    st = run_json(dbp, capsys, "sync-status")["sync_status"]
    assert st[0]["code"] == "S01" and st[0]["status"] == "ok"
    code, out, _ = run(dbp, capsys, "sync-status")
    assert code == 0 and "MÃ" in out and "S01" in out and "YouTube" in out


def test_sync_failed_prints_error(dbp, capsys, monkeypatch, freeze_time):
    freeze_time(vn(2026, 11, 18))
    _fake_collect(monkeypatch, "failed", "Chưa cài yt-dlp")
    add(dbp, capsys, "Lan", "1", "--youtube", YT)
    code, out, _ = run(dbp, capsys, "sync")
    assert code == 0 and "lỗi" in out and "Chưa cài yt-dlp" in out
    code, out, _ = run(dbp, capsys, "sync-status")
    assert "chưa cài yt-dlp".lower() in out.lower()
    code, out, _ = run(dbp, capsys, "sync", "S01", "--platform", "tiktok")
    assert code == 0 and "S01 Lan: (không có kênh" in out


def test_sync_status_empty(dbp, capsys):
    code, out, _ = run(dbp, capsys, "sync-status")
    assert code == 0 and "Chưa có kênh" in out


def _reports_count(dbp):
    con = db.connect(dbp)
    try:
        return con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    finally:
        con.close()


def test_report_without_send_stores_nothing(dbp, capsys, freeze_time):
    freeze_time(vn(2026, 11, 18))
    add(dbp, capsys, "Lan", "1")
    code, out, _ = run(dbp, capsys, "report")
    assert code == 0 and "Báo cáo đăng video tuần 09/11" in out
    assert _reports_count(dbp) == 0
    data = run_json(dbp, capsys, "report")
    assert data["week_start"] == "2026-11-09" and "text" in data and data["ai_used"] is False
    assert _reports_count(dbp) == 0
    code, out, _ = run(dbp, capsys, "report", "--week-offset", "0")
    assert "16/11" in out


def test_report_ai_note_on_stderr(dbp, capsys, freeze_time):
    freeze_time(vn(2026, 11, 18))
    add(dbp, capsys, "Lan", "1")
    code, out, err = run(dbp, capsys, "report", "--ai")
    assert code == 0 and "OPENAI_API_KEY" in err and "OPENAI_API_KEY" not in out


def test_report_send_stdout_marks_delivered_then_skips(dbp, capsys, freeze_time):
    freeze_time(vn(2026, 11, 18))
    add(dbp, capsys, "Lan", "1")
    code, out, _ = run(dbp, capsys, "report", "--send")
    assert code == 0 and "Báo cáo đăng video" in out and "Đã gửi báo cáo" in out
    con = db.connect(dbp)
    row = con.execute("SELECT delivered FROM reports").fetchone()
    con.close()
    assert row["delivered"] == 1

    code, out, _ = run(dbp, capsys, "report", "--send")
    assert code == 0 and "Bỏ qua" in out and "Báo cáo đăng video" not in out
    data = run_json(dbp, capsys, "report", "--send")
    assert data["skipped"] is True
    data = run_json(dbp, capsys, "report", "--send", "--force")  # stdout noise must not break JSON
    assert data["skipped"] is False and data["delivered"] is True


def test_report_send_failure_exit_1(dbp, capsys, freeze_time, monkeypatch):
    freeze_time(vn(2026, 11, 18))
    add(dbp, capsys, "Lan", "1")
    monkeypatch.setenv("TRACKER_DELIVER", "file")
    monkeypatch.setenv("TRACKER_DELIVER_FILE", "")
    code, out, err = run(dbp, capsys, "report", "--send")
    assert code == 1 and err.strip()


def test_serve_and_mcp_lazy_dispatch(dbp, capsys, monkeypatch):
    from video_tracker import http_api, mcp_server

    calls = []
    monkeypatch.setattr(http_api, "serve", lambda s, with_scheduler=False: calls.append(("serve", with_scheduler)))
    monkeypatch.setattr(mcp_server, "run_stdio", lambda s: calls.append(("mcp", s.db.name)))
    assert cli.main(["--db", dbp, "serve", "--scheduler"]) == 0
    assert cli.main(["--db", dbp, "serve"]) == 0
    assert cli.main(["--db", dbp, "mcp"]) == 0
    assert calls == [("serve", True), ("serve", False), ("mcp", "t.sqlite3")]


# ---------------------------------------------------------------------------
# collect-push
# ---------------------------------------------------------------------------

STUDENTS = [
    {"id": 1, "code": "S01", "name": "Lan", "weekly_goal": 2, "tiktok_url": TT, "youtube_url": YT, "active": 1},
    {"id": 2, "code": "S02", "name": "Minh", "weekly_goal": 2, "tiktok_url": TT, "youtube_url": "", "active": 1},
    {"id": 3, "code": "S03", "name": "Hoa", "weekly_goal": 2, "tiktok_url": "", "youtube_url": YT, "active": 0},
]


def _targets(students):
    """What GET /ingest/targets returns for these students (active only, no names)."""
    return [{"code": s["code"], "platform": p, "channel_url": s[f"{p}_url"]}
            for s in students if s["active"] for p in ("tiktok", "youtube") if s[f"{p}_url"]]


def _server(monkeypatch, students=STUDENTS, students_status=200, ingest_status=200):
    seen = {"posts": [], "auth": set(), "paths": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"].add(request.headers.get("authorization"))
        seen["paths"].append(request.url.path)
        if request.url.path == "/ingest/targets":
            if students_status != 200:
                return httpx.Response(students_status, json={"error": "Sai token."})
            return httpx.Response(200, json=_targets(students))
        if request.url.path == "/ingest":
            seen["posts"].append(json.loads(request.content))
            return httpx.Response(ingest_status, json={"ok": True} if ingest_status == 200 else {"error": "Lỗi."})
        return httpx.Response(404)

    monkeypatch.setattr(cli, "_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=30))
    return seen


def test_collect_push_success(dbp, capsys, monkeypatch, freeze_time):
    freeze_time(vn(2026, 11, 18))
    monkeypatch.setenv("TRACKER_API_TOKEN", TOKEN)
    _fake_collect(monkeypatch)
    seen = _server(monkeypatch)
    code, out, err = run(dbp, capsys, "collect-push", "--server", "https://tracker.example.com/")
    assert code == 0 and "S01 · YouTube: đã đẩy" in out
    assert len(seen["posts"]) == 1  # youtube only, inactive skipped
    assert "/api/students" not in seen["paths"] and seen["paths"][0] == "/ingest/targets"
    body = seen["posts"][0]
    assert body["student"] == "S01" and body["collector"] == "mac" and body["platform"] == "youtube"
    assert body["channel_url"] == YT and body["segments"] == []
    assert body["status"] == "ok" and len(body["videos"]) == 1
    assert body["started_at"].endswith("+07:00")
    assert seen["auth"] == {f"Bearer {TOKEN}"}
    assert TOKEN not in out + err


def test_collect_push_tiktok_wrapped_json_localhost_http(dbp, capsys, monkeypatch, freeze_time):
    freeze_time(vn(2026, 11, 18))
    monkeypatch.setenv("TRACKER_API_TOKEN", TOKEN)
    _fake_collect(monkeypatch)
    seen = _server(monkeypatch)
    data = run_json(dbp, capsys, "collect-push", "--server", "http://localhost:8765",
                    "--platform", "tiktok", "--limit", "5")
    assert data["pushed"] == 2 and data["failed"] == 0
    assert {p["student"] for p in seen["posts"]} == {"S01", "S02"}
    assert all(p["fetch_limit"] == 5 for p in seen["posts"])


def test_collect_push_ingest_failure_exit_1(dbp, capsys, monkeypatch, freeze_time):
    freeze_time(vn(2026, 11, 18))
    monkeypatch.setenv("TRACKER_API_TOKEN", TOKEN)
    _fake_collect(monkeypatch)
    _server(monkeypatch, ingest_status=400)
    code, out, err = run(dbp, capsys, "collect-push", "--server", "https://tracker.example.com")
    assert code == 1 and "ĐẨY LỖI" in out and TOKEN not in out + err


def test_collect_push_401_from_server(dbp, capsys, monkeypatch):
    monkeypatch.setenv("TRACKER_API_TOKEN", TOKEN)
    _server(monkeypatch, students_status=401)
    code, out, err = run(dbp, capsys, "collect-push", "--server", "https://tracker.example.com")
    assert code == 1 and "từ chối token" in err and TOKEN not in out + err


def test_collect_push_refuses_plain_http_remote(dbp, capsys, monkeypatch):
    monkeypatch.setenv("TRACKER_API_TOKEN", TOKEN)
    seen = _server(monkeypatch)
    code, out, err = run(dbp, capsys, "collect-push", "--server", "http://tracker.example.com")
    assert code == 1 and "https" in err and not seen["auth"]
    assert TOKEN not in out + err


def test_collect_push_missing_token(dbp, capsys, monkeypatch):
    seen = _server(monkeypatch)
    code, out, err = run(dbp, capsys, "collect-push", "--server", "https://tracker.example.com")
    assert code == 1 and "TRACKER_API_TOKEN" in err and not seen["auth"]


def test_collect_push_no_token_flag(dbp, capsys):
    code = cli.main(["--db", dbp, "collect-push", "--server", "https://x.example.com", "--token", "abc"])
    assert code == 2
    assert "abc" not in capsys.readouterr().out


def test_collect_push_connection_error_hides_token(dbp, capsys, monkeypatch):
    monkeypatch.setenv("TRACKER_API_TOKEN", TOKEN)

    def handler(request):
        raise httpx.ConnectError("boom " + TOKEN)

    monkeypatch.setattr(cli, "_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    code, out, err = run(dbp, capsys, "collect-push", "--server", "https://tracker.example.com")
    assert code == 1 and "ConnectError" in err and TOKEN not in out + err


def test_module_entrypoint(dbp):
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "video_tracker", "--db", dbp, "list", "--json"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout) == {"students": []}


def test_progress_and_history_show_unreadable_posts(dbp, capsys, freeze_time):
    freeze_time(vn(2026, 11, 18))
    add(dbp, capsys, "Lan", "2", "--tiktok", TT)
    con = db.connect(dbp)
    try:
        from video_tracker import core
        core.upsert_observed_videos(con, 1, "tiktok", [
            {"video_key": "7000000000000000001", "url": TT + "/video/7000000000000000001",
             "title": "", "published_date": "2026-11-17", "countable": False}], "observed")
    finally:
        con.close()
    suffix = " (+1 bài đăng không đọc được, có thể là bài ảnh)"
    code, out, _ = run(dbp, capsys, "progress")
    assert code == 0 and "S01 Lan 0/2 (TikTok: chưa đồng bộ lần nào)" + suffix in out
    assert "chưa đăng" not in out.lower()
    code, out, _ = run(dbp, capsys, "history", "S01", "--weeks", "2")
    lines = [ln for ln in out.splitlines() if "2026-11-16 →" in ln]
    assert len(lines) == 1 and lines[0].endswith(suffix) and "0/2" in lines[0]
    assert sum(1 for ln in out.splitlines() if ln.endswith(suffix)) == 1
    data = run_json(dbp, capsys, "progress")
    assert data["students"][0]["unreadable_posts"] == 1
    assert data["students"][0]["videos"][0]["countable"] is False


def test_collect_push_forwards_countable(dbp, capsys, monkeypatch, freeze_time):
    freeze_time(vn(2026, 11, 18))
    monkeypatch.setenv("TRACKER_API_TOKEN", TOKEN)

    def fake(platform, channel_url, limit, settings, http_client=None):
        vids = [
            {"video_key": "7000000000000000001", "url": TT + "/video/7000000000000000001",
             "title": "t", "published_date": "2026-11-17"},
            {"video_key": "7000000000000000002", "url": TT + "/video/7000000000000000002",
             "title": "", "published_date": "2026-11-16", "countable": False},
        ]
        return collect.CollectResult(
            platform=platform, collector="yt-dlp", status="ok", fetch_limit=limit,
            fetched_count=2, videos=vids, error="",
            started_at=vn(2026, 11, 18), finished_at=vn(2026, 11, 18))

    monkeypatch.setattr(collect, "collect_channel", fake)
    seen = _server(monkeypatch, students=[STUDENTS[1]])
    code, _, err = run(dbp, capsys, "collect-push", "--server", "https://tracker.example.com",
                       "--platform", "tiktok")
    assert code == 0, err
    videos = seen["posts"][0]["videos"]
    assert [v["countable"] for v in videos] == [True, False]
