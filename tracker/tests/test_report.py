"""Tests for video_tracker.report (no network)."""

from __future__ import annotations

import json
import sys

import httpx
import pytest

from conftest import vn
from video_tracker import config, core, report

KEY = "sk-test-SECRET-123"
STARTED = "2026-11-17T09:00:00+07:00"
FINISHED = "2026-11-17T10:00:00+07:00"


@pytest.fixture(autouse=True)
def _clock(freeze_time):
    # Wednesday; week_offset=-1 is 2026-11-09 .. 2026-11-15
    freeze_time(vn(2026, 11, 18))


def sync(con, student, status="ok", fetched=5, limit=30, dates=(), platform="tiktok"):
    core.record_sync_run(
        con, student["id"], platform, "yt-dlp", status, STARTED, FINISHED, limit, fetched,
        list(dates), "lỗi" if status == "failed" else "",
        channel_url=student[f"{platform}_url"],
    )


def tt(name, n):
    return f"https://www.tiktok.com/@{name}/video/{7000000000000000000 + n}"


def add_video(con, student, n, day="2026-11-10"):
    core.record_video(con, student["id"], tt(student["code"].lower(), n), day, title=f"Tiêu đề bí mật {n}")


def mixed_class(con):
    lan = core.add_student(con, "Lan", 2, tiktok_url="https://www.tiktok.com/@lan")
    add_video(con, lan, 1)
    add_video(con, lan, 2, "2026-11-11")
    sync(con, lan)
    minh = core.add_student(con, "Minh", 3, tiktok_url="https://www.tiktok.com/@minh")
    add_video(con, minh, 3)
    sync(con, minh)
    hoa = core.add_student(con, "Hoa", 3, tiktok_url="https://www.tiktok.com/@hoa")
    add_video(con, hoa, 4)
    sync(con, hoa, fetched=30, dates=["2026-11-10"] * 30)
    core.add_student(con, "Nam", 2, tiktok_url="https://www.tiktok.com/@nam")
    core.add_student(con, "Bảo", 2)
    cuong = core.add_student(con, "Cường", 2, tiktok_url="https://www.tiktok.com/@cuong")
    sync(con, cuong, status="failed", fetched=0)
    return {s["name"]: s for s in (lan, minh, hoa, cuong)}


def mk(**kw):
    return config.Settings(**kw)


def test_mixed_class_text(con, settings):
    mixed_class(con)
    res = report.build_report(con, settings, week_offset=-1)
    text = res["text"]
    assert res["week_start"] == "2026-11-09" and res["week_end"] == "2026-11-15"
    assert res["ai_used"] is False and res["ai_note"] == ""
    assert text.startswith("📊 Báo cáo đăng video tuần 09/11–15/11 (giờ VN)")
    order = [
        "Đủ mục tiêu", "Ít nhất (có thể còn video chưa đọc tới)",
        "Thiếu theo dữ liệu hiện có", "Chưa đủ dữ liệu để kết luận",
    ]
    positions = [text.index(o) for o in order]
    assert positions == sorted(positions)
    assert "• Lan 2/2" in text
    assert "• Hoa 1/3" in text
    assert "• Minh 1/3" in text
    assert "• Nam 0/2 (TikTok: chưa đồng bộ lần nào)" in text
    assert "• Bảo 0/2 (chưa khai báo kênh)" in text
    assert "• Cường 0/2 (TikTok: lần đồng bộ gần nhất lỗi)" in text
    assert text.endswith(core.DISCLAIMER)
    assert "|" not in text
    assert len(text) <= report.MAX_CHARS
    # forbidden words never appear anywhere in the deterministic text, disclaimer included
    lowered = text.lower()
    for phrase in report.FORBIDDEN_PHRASES:
        assert phrase not in lowered


def test_stale_reason_text(con, settings):
    s = core.add_student(con, "Yên", 2, youtube_url="https://www.youtube.com/@yen")
    core.record_sync_run(
        con, s["id"], "youtube", "yt-dlp", "ok", "2026-11-10T09:00:00+07:00",
        "2026-11-10T10:00:00+07:00", 30, 3, [], "", channel_url=s["youtube_url"],
    )
    text = report.build_report(con, settings, week_offset=-1)["text"]
    assert "• Yên 0/2 (YouTube: dữ liệu đã cũ)" in text


def test_empty_groups_omitted(con, settings):
    lan = core.add_student(con, "Lan", 1, tiktok_url="https://www.tiktok.com/@lan")
    add_video(con, lan, 1)
    sync(con, lan)
    text = report.build_report(con, settings, week_offset=-1)["text"]
    assert "Đủ mục tiêu" in text
    for gone in ("Ít nhất", "Thiếu theo dữ liệu", "Chưa đủ dữ liệu để kết luận"):
        assert gone not in text


def test_no_students(con, settings):
    text = report.build_report(con, settings, week_offset=-1)["text"]
    assert "Chưa có học viên nào" in text
    assert text.endswith(core.DISCLAIMER)


def test_long_report_split_into_parts(con, settings):
    # A8: nothing is dropped; the full text is kept and chat parts are each <= MAX_CHARS.
    for i in range(80):
        s = core.add_student(con, f"Học viên số {i:02d}", 1, tiktok_url=f"https://www.tiktok.com/@u{i}")
        add_video(con, s, 100 + i)
        sync(con, s)
    for i in range(60):
        core.add_student(con, f"Thiếu dữ liệu {i:02d}", 2, tiktok_url=f"https://www.tiktok.com/@n{i}")
    res = report.build_report(con, settings, week_offset=-1)
    text, parts = res["text"], res["parts"]
    assert text.count("• Học viên số") == 80 and text.count("• Thiếu dữ liệu") == 60
    assert len(parts) >= 2
    assert all(len(p) <= report.MAX_CHARS for p in parts)
    n = len(parts)
    assert all(p.startswith(f"({i}/{n}) ") for i, p in enumerate(parts, start=1))
    assert "📊 Báo cáo đăng video tuần" in parts[0]
    assert all("📊" not in p for p in parts[1:])
    assert parts[-1].endswith(core.DISCLAIMER)
    assert all(core.DISCLAIMER not in p for p in parts[:-1])
    joined = "\n".join(parts)
    assert joined.count("• Học viên số") == 80 and joined.count("• Thiếu dữ liệu") == 60
    # Every student line is whole inside one part (split only on line boundaries).
    for line in text.splitlines():
        if line.startswith("• "):
            assert any(line in p.splitlines() for p in parts)
    assert "(tiếp)" in joined


def test_short_report_single_part_without_marker(con, settings):
    mixed_class(con)
    res = report.build_report(con, settings, week_offset=-1)
    assert res["parts"] == [res["text"]]


def test_anonymized_payload_has_no_pii(con, settings):
    mixed_class(con)
    progress = core.week_progress(con, -1, settings)
    payload = report.anonymized_payload(progress)
    blob = json.dumps(payload, ensure_ascii=False)
    for secret in ("Lan", "Minh", "Hoa", "Nam", "Bảo", "Cường", "tiktok.com", "http", "Tiêu đề bí mật"):
        assert secret not in blob
    assert set(payload["students"][0]) == {"code", "weekly_goal", "count", "unreadable_posts", "status", "coverage", "by_source"}
    assert payload["students"][0]["code"] == "S01"
    assert "summary" in payload and payload["week_start"] == "2026-11-09"


def ai_client(content="Nhận xét", status=200, capture=None, exc=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["url"] = str(request.url)
            capture["auth"] = request.headers.get("authorization")
            capture["body"] = json.loads(request.content)
        if exc:
            raise exc(request)
        if status != 200:
            return httpx.Response(status, text=f"boom {KEY}")
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def ai_settings(**kw):
    base = dict(ai=True, openai_api_key=KEY, openai_model="gpt-test", openai_base_url="https://llm.test/v1/")
    base.update(kw)
    return mk(**base)


def test_ai_success_renders_only_fixed_action_templates(con):
    # 10 students so S1x / S10 exist alongside S01
    for i in range(10):
        s = core.add_student(con, f"Tên{i + 1}", 2, tiktok_url=f"https://www.tiktok.com/@u{i}")
        sync(con, s)
    cap = {}
    client = ai_client('{"actions":["check_channels","offer_support"]}', capture=cap)
    res = report.build_report(con, ai_settings(), week_offset=-1, http_client=client)
    assert res["ai_used"] is True and res["ai_note"] == ""
    assert "Nhận xét (AI):" in res["text"]
    assert report.AI_ACTIONS["check_channels"] in res["text"]
    assert report.AI_ACTIONS["offer_support"] in res["text"]
    assert res["text"].index("Nhận xét (AI):") < res["text"].index(core.DISCLAIMER)
    assert cap["url"] == "https://llm.test/v1/chat/completions"
    assert cap["auth"] == f"Bearer {KEY}"
    assert cap["body"]["model"] == "gpt-test"
    assert [m["role"] for m in cap["body"]["messages"]] == ["system", "user"]
    assert "Tên1" not in json.dumps(cap["body"], ensure_ascii=False)
    sysmsg = cap["body"]["messages"][0]["content"]
    assert "check_channels" in sysmsg and '"actions"' in sysmsg


def test_ai_not_requested_has_empty_note(con):
    mixed_class(con)
    s = ai_settings(ai=False)
    res = report.build_report(con, s, week_offset=-1, http_client=ai_client(status=500))
    assert res["ai_used"] is False and res["ai_note"] == ""


def test_use_ai_true_overrides_setting(con):
    mixed_class(con)
    res = report.build_report(con, ai_settings(ai=False), week_offset=-1, use_ai=True,
                              http_client=ai_client('{"actions":["offer_support"]}'))
    assert res["ai_used"] is True
    res = report.build_report(con, ai_settings(ai=True), week_offset=-1, use_ai=False,
                              http_client=ai_client("S01 tốt"))
    assert res["ai_used"] is False


@pytest.mark.parametrize("field,expect", [("openai_api_key", "OPENAI_API_KEY"), ("openai_model", "TRACKER_OPENAI_MODEL")])
def test_ai_missing_key_or_model(con, field, expect):
    mixed_class(con)
    called = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: called.append(1) or httpx.Response(200)))
    res = report.build_report(con, ai_settings(**{field: ""}), week_offset=-1, http_client=client)
    assert res["ai_used"] is False and expect in res["ai_note"]
    assert not called
    assert "Nhận xét (AI)" not in res["text"]


def test_ai_http_500_still_reports_and_hides_key(con):
    mixed_class(con)
    res = report.build_report(con, ai_settings(), week_offset=-1, http_client=ai_client(status=500))
    assert res["ai_used"] is False and "500" in res["ai_note"]
    assert KEY not in res["ai_note"] and KEY not in res["text"]
    assert "• Lan 2/2" in res["text"]


def test_ai_timeout_still_reports(con):
    mixed_class(con)
    client = ai_client(exc=lambda req: httpx.ReadTimeout(f"timeout {KEY}", request=req))
    res = report.build_report(con, ai_settings(), week_offset=-1, http_client=client)
    assert res["ai_used"] is False and "quá thời gian" in res["ai_note"]
    assert KEY not in res["ai_note"]


def test_ai_commentary_exception_has_no_key():
    client = ai_client(exc=lambda req: httpx.ConnectError(f"cannot {KEY}", request=req))
    with pytest.raises(RuntimeError) as err:
        report.ai_commentary({"students": []}, ai_settings(), http_client=client)
    assert KEY not in str(err.value)
    client = ai_client(status=500)
    with pytest.raises(RuntimeError) as err:
        report.ai_commentary({"students": []}, ai_settings(), http_client=client)
    assert KEY not in str(err.value)


def test_ai_bad_payload_shape():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"x": 1})))
    with pytest.raises(RuntimeError):
        report.ai_commentary({}, ai_settings(), http_client=client)


@pytest.mark.parametrize("layer", ["content", "response"])
def test_ai_deeply_nested_json_still_generates_report(con, layer):
    core.add_student(con, "Lan", 3)
    depth = sys.getrecursionlimit() + 100
    nested = "[" * depth + "0" + "]" * depth
    if layer == "content":
        client = ai_client(nested)
    else:
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=nested.encode())
        ))
    with client:
        result = report.build_report(con, ai_settings(), week_offset=0, http_client=client)
    assert result["ai_used"] is False and result["ai_note"]
    assert "định dạng" in result["ai_note"]
    assert "Nhận xét (AI):" not in result["text"]
    assert "• Lan 0/3" in result["text"]
    assert result["text"].endswith(core.DISCLAIMER)


@pytest.mark.parametrize("bad", ["S01 chưa đăng video", "S01 KHÔNG ĐĂNG bài", "Chưa Đăng"])
def test_forbidden_phrase_dropped_when_insufficient(con, bad):
    mixed_class(con)  # includes insufficient_data students
    res = report.build_report(con, ai_settings(), week_offset=-1, http_client=ai_client(bad))
    assert res["ai_used"] is False
    assert res["ai_note"]
    assert "Nhận xét (AI)" not in res["text"]


def test_accusation_dropped_even_without_insufficient(con):
    # A6: the accusation check is unconditional (it used to apply only with insufficient data).
    lan = core.add_student(con, "Lan", 1, tiktok_url="https://www.tiktok.com/@lan")
    add_video(con, lan, 1)
    sync(con, lan)
    res = report.build_report(con, ai_settings(), week_offset=-1,
                              http_client=ai_client("Không có ai chưa đăng, S01 tốt"))
    assert res["ai_used"] is False and res["ai_note"]
    assert "Nhận xét (AI)" not in res["text"]


# ---- send_report ----------------------------------------------------------


def hook_settings(tmp_path, **kw):
    base = dict(db=tmp_path / "x.sqlite3", deliver="webhook", deliver_webhook_url="https://hook.test/x")
    base.update(kw)
    return mk(**base)


def counting_client(status=200):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(status, json={})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def row(con):
    return con.execute("SELECT * FROM reports WHERE week_start='2026-11-09' AND kind='weekly'").fetchone()


def test_send_report_idempotent_and_force(con, tmp_path):
    mixed_class(con)
    s = hook_settings(tmp_path)
    client, calls = counting_client()
    first = report.send_report(con, s, week_offset=-1, http_client=client)
    assert first["skipped"] is False and first["delivered"] is True
    assert len(calls) == 1 and calls[0]["text"] == first["text"]
    r = row(con)
    assert r["delivered"] == 1 and r["body"] == first["text"]

    again = report.send_report(con, s, week_offset=-1, http_client=client)
    assert again["skipped"] is True and again["week_start"] == "2026-11-09"
    assert len(calls) == 1

    forced = report.send_report(con, s, week_offset=-1, force=True, http_client=client)
    assert forced["skipped"] is False and len(calls) == 2
    assert con.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1


def test_send_report_failed_delivery_can_retry(con, tmp_path):
    mixed_class(con)
    s = hook_settings(tmp_path)
    bad, bad_calls = counting_client(500)
    res = report.send_report(con, s, week_offset=-1, http_client=bad)
    assert res["delivered"] is False and res["deliver"]["ok"] is False
    r = row(con)
    assert r["delivered"] == 0 and r["body"]

    good, good_calls = counting_client(200)
    res = report.send_report(con, s, week_offset=-1, http_client=good)
    assert res["skipped"] is False and res["delivered"] is True
    assert len(good_calls) == 1 and row(con)["delivered"] == 1


def test_send_report_zalo_ok_false_keeps_undelivered(con, tmp_path):
    mixed_class(con)
    s = mk(deliver="zalo_mod", zalo_mod_token="t", zalo_target_id="g1")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": False})))
    res = report.send_report(con, s, week_offset=-1, http_client=client)
    assert res["delivered"] is False and row(con)["delivered"] == 0


def test_send_report_stdout(con, tmp_path, capsys):
    mixed_class(con)
    res = report.send_report(con, mk(deliver="stdout"), week_offset=-1)
    assert res["delivered"] is True
    assert "Báo cáo đăng video tuần" in capsys.readouterr().out


def _unreadable(con, student, n, day="2026-11-12"):
    vid = str(7000000000000000000 + n)
    core.upsert_observed_videos(con, student["id"], "tiktok", [{
        "video_key": vid, "url": f"https://www.tiktok.com/@x/video/{vid}", "title": "",
        "published_date": day, "countable": False}], "observed")


def test_unreadable_posts_suffix_and_payload(con, settings):
    lan = core.add_student(con, "Lan", 2, tiktok_url="https://www.tiktok.com/@lan")
    add_video(con, lan, 1)
    _unreadable(con, lan, 50)
    _unreadable(con, lan, 51)
    sync(con, lan)
    nam = core.add_student(con, "Nam", 2, tiktok_url="https://www.tiktok.com/@nam")
    _unreadable(con, nam, 60)
    minh = core.add_student(con, "Minh", 1, tiktok_url="https://www.tiktok.com/@minh")
    add_video(con, minh, 2)
    sync(con, minh)
    res = report.build_report(con, settings, week_offset=-1)
    text = res["text"]
    assert "• Lan 1/2 (+2 bài đăng không đọc được, có thể là bài ảnh)" in text
    assert ("• Nam 0/2 (TikTok: chưa đồng bộ lần nào) "
            "(+1 bài đăng không đọc được, có thể là bài ảnh)") in text
    assert "• Minh 1/1\n" in text + "\n" and "Minh 1/1 (+" not in text
    for phrase in report.FORBIDDEN_PHRASES:
        assert phrase not in text.lower()
    payload = report.anonymized_payload(res["progress"])
    by_code = {s["code"]: s for s in payload["students"]}
    assert by_code["S01"]["unreadable_posts"] == 2 and by_code["S01"]["count"] == 1
    assert by_code["S02"]["unreadable_posts"] == 1 and by_code["S03"]["unreadable_posts"] == 0
    assert "tiktok.com" not in json.dumps(payload)


def test_system_prompt_requests_only_action_codes():
    assert all(action in report.SYSTEM_PROMPT for action in report.AI_ACTIONS)
    assert "không thêm trường hoặc văn bản" in report.SYSTEM_PROMPT
