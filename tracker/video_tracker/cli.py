"""Dòng lệnh `tracker`: quản lý học viên, đồng bộ, báo cáo, chạy dịch vụ."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from video_tracker import collect, config, core, db, metrics, report

PLATFORMS = ("tiktok", "youtube")
LOCAL_HOSTS = ("localhost", "127.0.0.1")
PUSH_TIMEOUT = 30

_STATUS_TITLES = {
    "met": "Đủ mục tiêu",
    "at_least": "Ít nhất đạt (có thể còn video chưa đọc tới)",
    "below_goal": "Thiếu theo dữ liệu hiện có",
    "insufficient_data": "Chưa đủ dữ liệu để kết luận",
}
_RUN_STATUS = {"ok": "ok", "partial": "một phần", "failed": "lỗi"}
_PLATFORM_NAMES = {"tiktok": "TikTok", "youtube": "YouTube"}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _emit_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, default=str))


@contextlib.contextmanager
def _connection(settings: config.Settings):
    con = db.connect(settings.db)
    try:
        yield con
    finally:
        con.close()


def _http_client() -> httpx.Client:
    """Client HTTP cho collect-push (test thay bằng client dùng MockTransport)."""
    return httpx.Client(timeout=PUSH_TIMEOUT)


def _student_line(student: dict) -> str:
    channels = []
    for platform in PLATFORMS:
        if student.get(f"{platform}_url"):
            channels.append(f"{_PLATFORM_NAMES[platform]}: {student[f'{platform}_url']}")
    text = (f"{student['code']}  {student['name']}  mục tiêu {student['weekly_goal']}/tuần  "
            f"{' | '.join(channels) if channels else '(chưa có kênh)'}")
    if not student.get("active", 1):
        text += "  [ngừng theo dõi]"
    return text


# ---------------------------------------------------------------------------
# Student commands
# ---------------------------------------------------------------------------

def cmd_add_student(args, settings) -> int:
    with _connection(settings) as con:
        student = core.add_student(con, args.name, args.goal, args.tiktok or "", args.youtube or "")
    if args.json:
        _emit_json(student)
    else:
        _out(f"Đã thêm học viên {student['code']}.")
        _out(_student_line(student))
    return 0


def cmd_list(args, settings) -> int:
    with _connection(settings) as con:
        students = core.list_students(con, include_inactive=args.all)
    if args.json:
        _emit_json({"students": students})
    elif not students:
        _out("Chưa có học viên nào.")
    else:
        for student in students:
            _out(_student_line(student))
    return 0


def cmd_update(args, settings) -> int:
    fields: dict[str, Any] = {}
    if args.name is not None:
        fields["name"] = args.name
    if args.goal is not None:
        fields["weekly_goal"] = args.goal
    if args.tiktok is not None:
        fields["tiktok_url"] = args.tiktok
    if args.youtube is not None:
        fields["youtube_url"] = args.youtube
    if args.activate:
        fields["active"] = 1
    if args.deactivate:
        fields["active"] = 0
    if not fields:
        raise ValueError("Chưa có gì để cập nhật (dùng --name, --goal, --tiktok, --youtube, "
                         "--activate hoặc --deactivate).")
    with _connection(settings) as con:
        student = core.update_student(con, args.ref, **fields)
    if args.json:
        _emit_json(student)
    else:
        _out(f"Đã cập nhật {student['code']}.")
        _out(_student_line(student))
    return 0


def cmd_deactivate(args, settings) -> int:
    with _connection(settings) as con:
        student = core.update_student(con, args.ref, active=0)
    if args.json:
        _emit_json(student)
    else:
        _out(f"Đã ngừng theo dõi {student['code']} ({student['name']}). Dữ liệu cũ vẫn được giữ.")
    return 0


def cmd_record(args, settings) -> int:
    with _connection(settings) as con:
        student = core.get_student(con, args.ref)
        video = core.record_video(con, student["id"], args.url, args.date, args.title or "")
    if args.json:
        _emit_json({"student": student, "video": video})
    else:
        _out(f"Đã ghi video cho {student['code']} ({student['name']}): "
             f"{video['platform']} {video['published_date']} {video['url']}")
    return 0


# ---------------------------------------------------------------------------
# Sync / progress / history
# ---------------------------------------------------------------------------

def cmd_sync(args, settings) -> int:
    platforms = [args.platform] if args.platform else None
    with _connection(settings) as con:
        if args.ref:
            student = core.get_student(con, args.ref)
            results = collect.sync_student(con, student, settings, platforms)
            totals = {"runs": 0, "ok": 0, "partial": 0, "failed": 0, "added": 0}
            for r in results:
                totals["runs"] += 1
                totals[r["status"]] += 1
                totals["added"] += r["added"]
            summary = {"students": [{"code": student["code"], "name": student["name"],
                                     "results": results}], "totals": totals}
        else:
            summary = collect.sync_all(con, settings, platforms)
    if args.json:
        _emit_json(summary)
        return 0
    for entry in summary["students"]:
        if not entry["results"]:
            _out(f"{entry['code']} {entry['name']}: (không có kênh để đồng bộ)")
        for r in entry["results"]:
            line = (f"{entry['code']} {entry['name']} · {_PLATFORM_NAMES.get(r['platform'], r['platform'])}: "
                    f"{_RUN_STATUS.get(r['status'], r['status'])}, đọc {r['fetched_count']}, "
                    f"thêm mới {r['added']}")
            if r.get("error"):
                line += f" — {r['error']}"
            _out(line)
    t = summary["totals"]
    _out(f"Tổng: {t['runs']} lượt (ok {t['ok']}, một phần {t['partial']}, lỗi {t['failed']}), "
         f"thêm {t['added']} video.")
    return 0


def _coverage_note(student: dict) -> str:
    notes = []
    for platform, info in student.get("platforms", {}).items():
        cov = info.get("coverage", "")
        if cov and cov != "complete":
            reason = f" ({info['reason']})" if info.get("reason") else ""
            notes.append(f"{_PLATFORM_NAMES.get(platform, platform)} {cov}{reason}")
    return "; ".join(notes)


def _progress_student_line(student: dict) -> str:
    line = f"  • {student['code']} {student['name']} {student['count']}/{student['weekly_goal']}"
    if student["status"] == "insufficient_data":
        reasons = [report._reason_text(r) for r in student.get("reasons", [])]
        if reasons:
            line += f" ({', '.join(reasons)})"
    elif student["status"] == "at_least":
        line += " (ít nhất)"
    return line + report.unreadable_suffix(student)


def cmd_progress(args, settings) -> int:
    with _connection(settings) as con:
        progress = core.week_progress(con, args.week_offset, settings)
    if args.json:
        _emit_json(progress)
        return 0
    suffix = " (tuần hiện tại)" if progress["is_current"] else ""
    _out(f"Tuần {progress['week_start']} → {progress['week_end']}{suffix}")
    s = progress["summary"]
    _out(f"Tổng {s['students']} học viên: đủ {s['met']}, ít nhất {s['at_least']}, "
         f"thiếu {s['below_goal']}, chưa đủ dữ liệu {s['insufficient_data']}")
    for status, title in _STATUS_TITLES.items():
        members = [st for st in progress["students"] if st["status"] == status]
        if not members:
            continue
        _out("")
        _out(f"{title} ({len(members)})")
        for st in members:
            _out(_progress_student_line(st))
    _out("")
    _out(progress["disclaimer"])
    return 0


def cmd_history(args, settings) -> int:
    with _connection(settings) as con:
        history = core.student_history(con, args.ref, args.weeks, settings)
    if args.json:
        _emit_json(history)
        return 0
    student = history["student"]
    _out(f"{student['code']} {student['name']} — mục tiêu {student['weekly_goal']}/tuần")
    for week in history["weeks"]:
        line = (f"  {week['week_start']} → {week['week_end']}: {week['count']}/{week['weekly_goal']} "
                f"· {week['status_label']}")
        note = _coverage_note(week)
        if note:
            line += f" [{note}]"
        _out(line + report.unreadable_suffix(week))
    return 0


def cmd_metrics(args, settings) -> int:
    with _connection(settings) as con:
        data = metrics.history(con, args.ref, args.period, args.count, args.offset)
    if args.json:
        _emit_json(data)
        return 0
    _out(f"{data['student']['code']} {data['student']['name']} — chỉ số theo "
         f"{'ngày' if args.period == 'day' else 'tuần'} (giờ VN)")
    _out("Thay đổi so với lần đo cuối của kỳ liền trước; thiếu số liệu thì không tính.")
    if not data["videos"]:
        _out("Chưa có video được ghi nhận.")
    labels = {"view_count": "xem", "like_count": "thích", "comment_count": "bình luận"}
    for video in data["videos"]:
        _out(f"\n{video['platform']} {video['published_date']} {video['url']}")
        for item in video["periods"]:
            values = []
            for field in metrics.FIELDS:
                value, delta = item["values"][field], item["change"][field]
                shown = str(value) if value is not None else "thiếu dữ liệu"
                change = f"{delta:+d}" if delta is not None else "thiếu dữ liệu"
                values.append(f"{labels[field]}: {shown} (thay đổi: {change})")
            _out(f"  {item['start']} → {item['end']}: " + "; ".join(values))
            _out(f"    kỳ trước: {item['baseline_at'] or 'thiếu dữ liệu'}; "
                 f"lần đo: {item['observed_at'] or 'thiếu dữ liệu'}")
    return 0


def cmd_sync_status(args, settings) -> int:
    with _connection(settings) as con:
        rows = core.sync_status(con)
    if args.json:
        _emit_json({"sync_status": rows})
        return 0
    if not rows:
        _out("Chưa có kênh nào được khai báo.")
        return 0
    table = [("MÃ", "NỀN TẢNG", "TRẠNG THÁI", "LẦN CUỐI", "ĐỌC")]
    for r in rows:
        if r["synced"]:
            status = _RUN_STATUS.get(r["status"], r["status"])
            when = r["finished_at"][:16].replace("T", " ")
            fetched = f"{r['fetched_count']}/{r['fetch_limit']}"
        else:
            status, when, fetched = "chưa đồng bộ", "-", "-"
        table.append((r["code"], _PLATFORM_NAMES.get(r["platform"], r["platform"]), status, when, fetched))
    widths = [max(len(row[i]) for row in table) for i in range(5)]
    for row in table:
        _out("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    for r in rows:
        if r["error"]:
            _out(f"  ! {r['code']} {_PLATFORM_NAMES.get(r['platform'], r['platform'])}: {r['error']}")
    return 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def cmd_report(args, settings) -> int:
    if args.send:
        send_settings = dataclasses.replace(settings, ai=True) if args.ai else settings
        buf = io.StringIO()
        # Kênh stdout của deliver tự in; khi --json phải giữ stdout là một tài liệu JSON duy nhất.
        ctx = contextlib.redirect_stdout(buf) if args.json else contextlib.nullcontext()
        with _connection(settings) as con, ctx:
            result = core_send(con, send_settings, args)
        if args.json:
            _emit_json(result)
        elif result.get("skipped"):
            _out(f"Bỏ qua: {result['reason']}")
        elif result["delivered"]:
            _out(f"Đã gửi báo cáo tuần {result['week_start']} qua kênh "
                 f"{result['deliver'].get('channel', '?')}.")
        else:
            _err(f"Gửi báo cáo thất bại: {result['deliver'].get('detail', '')}")
        if args.ai and not result.get("skipped") and not result.get("ai_used") and result.get("ai_note"):
            _err(result["ai_note"])
        return 0 if (result.get("skipped") or result.get("delivered")) else 1
    with _connection(settings) as con:
        built = report.build_report(con, settings, args.week_offset, use_ai=True if args.ai else None)
    if args.json:
        _emit_json({k: built[k] for k in ("week_start", "week_end", "text", "ai_used", "ai_note")})
    else:
        _out(built["text"])
    if args.ai and not built["ai_used"] and built["ai_note"]:
        _err(built["ai_note"])
    return 0


def core_send(con, settings, args) -> dict:
    return report.send_report(con, settings, args.week_offset, force=args.force)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def cmd_serve(args, settings) -> int:
    from video_tracker import http_api  # nạp muộn: cần thư viện dịch vụ

    http_api.serve(settings, with_scheduler=args.scheduler)
    return 0


def cmd_mcp(args, settings) -> int:
    from video_tracker import mcp_server

    mcp_server.run_stdio(settings)
    return 0


# ---------------------------------------------------------------------------
# collect-push
# ---------------------------------------------------------------------------

def _check_server(server: str) -> str:
    parsed = urlparse((server or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("https", "http") or not host:
        raise ValueError("Địa chỉ máy chủ không hợp lệ (cần dạng https://host).")
    if parsed.username or parsed.password:
        raise ValueError("Địa chỉ máy chủ không được chứa tài khoản/mật khẩu.")
    if parsed.query or parsed.fragment:
        raise ValueError("Địa chỉ máy chủ không được chứa query hoặc fragment.")
    if parsed.scheme == "http" and host not in LOCAL_HOSTS:
        raise ValueError("Máy chủ phải dùng https (http chỉ được phép với localhost/127.0.0.1) "
                         "để token không bị lộ trên đường truyền.")
    return server.strip().rstrip("/")


def _iso(value: Any) -> str:
    return value.isoformat(timespec="seconds") if hasattr(value, "isoformat") else str(value or "")


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _http_problem(resp: httpx.Response, token: str) -> str:
    detail = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            detail = str(data.get("error") or "")
    except ValueError:
        pass
    if resp.status_code in (401, 403):
        base = "Máy chủ từ chối token (HTTP %d)" % resp.status_code
    elif resp.status_code == 503:
        base = "Máy chủ chưa cấu hình token (HTTP 503)"
    else:
        base = "Máy chủ trả lỗi HTTP %d" % resp.status_code
    return _scrub(f"{base}: {detail}" if detail else base, token)


def cmd_collect_push(args, settings) -> int:
    # Prefer the dedicated ingest credential; fall back to the API token.
    token = settings.ingest_token or settings.api_token
    if not token:
        raise ValueError("Thiếu biến môi trường TRACKER_INGEST_TOKEN (hoặc TRACKER_API_TOKEN); "
                         "không nhận token qua dòng lệnh.")
    server = _check_server(args.server)
    platforms = [args.platform] if args.platform else ["youtube"]
    limit = args.limit if args.limit is not None else settings.sync_limit
    if limit < 1:
        raise ValueError("--limit phải là số nguyên dương.")
    headers = {"Authorization": f"Bearer {token}"}

    results: list[dict] = []
    client = _http_client()
    try:
        try:
            resp = client.get(f"{server}/ingest/targets", headers=headers)
        except httpx.HTTPError as exc:
            raise ValueError(_scrub(f"Không kết nối được máy chủ ({type(exc).__name__}).", token)) from None
        if resp.status_code != 200:
            raise ValueError(_http_problem(resp, token))
        try:
            targets = resp.json()
        except ValueError:
            raise ValueError("Máy chủ trả danh sách kênh không phải JSON.") from None
        if not isinstance(targets, list):
            raise ValueError("Danh sách kênh từ máy chủ không đúng định dạng.")

        for target in targets:
            if not isinstance(target, dict):
                continue
            code = str(target.get("code") or "")
            platform = target.get("platform")
            channel = str(target.get("channel_url") or "").strip()
            if not code or platform not in platforms or not channel:
                continue
            entry = {"student": code, "platform": platform, "collect_status": "", "fetched": 0,
                     "pushed": False, "stored_status": "", "error": ""}
            results.append(entry)
            res = collect.collect_channel(platform, channel, limit, settings)
            entry["collect_status"] = res.status
            entry["fetched"] = res.fetched_count
            body = {
                "student": code, "platform": platform, "collector": "mac",
                # The channel actually read; the server refuses (409) if it changed meanwhile.
                "channel_url": res.channel_url or channel,
                "status": res.status, "fetch_limit": res.fetch_limit,
                "fetched_count": res.fetched_count, "error": res.error,
                "started_at": _iso(res.started_at), "finished_at": _iso(res.finished_at),
                "segments": [
                    {"name": seg["name"], "fetched_count": seg["fetched_count"],
                     "dates": list(seg["dates"])}
                    for seg in res.segments
                ],
                "videos": [
                    {"video_key": v["video_key"], "url": v["url"], "title": v.get("title", ""),
                     "published_date": v["published_date"],
                     "countable": bool(v.get("countable", True)),
                     "metrics": v.get("metrics")}
                    for v in res.videos
                ],
            }
            try:
                push = client.post(f"{server}/ingest", json=body, headers=headers)
            except httpx.HTTPError as exc:
                entry["error"] = f"Không kết nối được máy chủ ({type(exc).__name__})."
                continue
            if push.status_code // 100 != 2:
                entry["error"] = _http_problem(push, token)
                continue
            entry["pushed"] = True
            try:
                stored = push.json()
            except ValueError:
                stored = {}
            if isinstance(stored, dict):
                entry["stored_status"] = str(stored.get("status") or "")
            if res.error:
                entry["error"] = res.error
    finally:
        client.close()

    failed = [r for r in results if not r["pushed"]]
    if args.json:
        _emit_json({"server": server, "results": results,
                    "pushed": len(results) - len(failed), "failed": len(failed)})
    else:
        if not results:
            _out("Không có học viên nào có kênh phù hợp để đẩy.")
        for r in results:
            name = _PLATFORM_NAMES.get(r["platform"], r["platform"])
            state = "đã đẩy" if r["pushed"] else "ĐẨY LỖI"
            line = (f"{r['student']} · {name}: {state} "
                    f"(đọc {r['fetched']}, {_RUN_STATUS.get(r['collect_status'], r['collect_status'])})")
            if r["stored_status"] and r["stored_status"] != r["collect_status"]:
                line += f" → máy chủ ghi {_RUN_STATUS.get(r['stored_status'], r['stored_status'])}"
            if r["error"]:
                line += f" — {r['error']}"
            _out(line)
        _out(f"Tổng: {len(results) - len(failed)} thành công, {len(failed)} lỗi.")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Parser / main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="in một tài liệu JSON duy nhất")

    parser = argparse.ArgumentParser(prog="tracker", description="Theo dõi học viên đăng video hằng tuần.")
    parser.add_argument("--db", metavar="PATH", help="đường dẫn file SQLite (ghi đè TRACKER_DB)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="LỆNH")

    def add(name: str, help_: str, handler) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help_, description=help_)
        p.set_defaults(handler=handler)
        return p

    p = add("add-student", "thêm học viên", cmd_add_student)
    p.add_argument("name")
    p.add_argument("--goal", type=int, default=3, help="số video mỗi tuần (mặc định 3)")
    p.add_argument("--tiktok", default="")
    p.add_argument("--youtube", default="")

    p = add("list", "liệt kê học viên", cmd_list)
    p.add_argument("--all", action="store_true", help="gồm cả học viên đã ngừng theo dõi")

    p = add("update", "sửa học viên ('' để xóa kênh)", cmd_update)
    p.add_argument("ref", help="mã (S01) hoặc id")
    p.add_argument("--name")
    p.add_argument("--goal", type=int)
    p.add_argument("--tiktok")
    p.add_argument("--youtube")
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--activate", action="store_true")
    grp.add_argument("--deactivate", action="store_true")

    p = add("deactivate", "ngừng theo dõi học viên (không xóa dữ liệu)", cmd_deactivate)
    p.add_argument("ref")

    p = add("record", "ghi tay một video", cmd_record)
    p.add_argument("ref")
    p.add_argument("url")
    p.add_argument("--date", required=True, help="YYYY-MM-DD (giờ VN)")
    p.add_argument("--title", default="")

    p = add("sync", "đồng bộ video từ kênh", cmd_sync)
    p.add_argument("ref", nargs="?")
    p.add_argument("--platform", choices=PLATFORMS)

    p = add("progress", "tiến độ tuần", cmd_progress)
    p.add_argument("--week-offset", type=int, default=0)

    p = add("history", "lịch sử nhiều tuần của một học viên", cmd_history)
    p.add_argument("ref")
    p.add_argument("--weeks", type=int, default=4)

    p = add("metrics", "lịch sử view, like và bình luận của từng video", cmd_metrics)
    p.add_argument("ref", help="mã (S01) hoặc id")
    p.add_argument("--period", choices=("day", "week"), default="day")
    p.add_argument("--count", type=int, default=7, help="số kỳ cần xem (1–52)")
    p.add_argument("--offset", type=int, default=0, help="lùi N ngày hoặc tuần (0–5200)")

    add("sync-status", "lần đồng bộ gần nhất từng kênh", cmd_sync_status)

    p = add("report", "tạo/gửi báo cáo tuần", cmd_report)
    p.add_argument("--week-offset", type=int, default=-1)
    p.add_argument("--ai", action="store_true", help="thêm nhận xét AI (cần cấu hình)")
    p.add_argument("--send", action="store_true", help="gửi qua TRACKER_DELIVER và ghi bảng reports")
    p.add_argument("--force", action="store_true", help="gửi lại dù tuần này đã gửi")

    p = add("serve", "chạy HTTP API", cmd_serve)
    p.add_argument("--scheduler", action="store_true", help="chạy kèm lịch đồng bộ/báo cáo")

    add("mcp", "chạy MCP stdio", cmd_mcp)

    p = add("collect-push", "thu thập tại máy này rồi đẩy lên máy chủ (/ingest/targets → /ingest)",
            cmd_collect_push)
    p.add_argument("--server", required=True, help="https://host (http chỉ cho localhost)")
    p.add_argument("--platform", choices=PLATFORMS, help="mặc định chỉ youtube")
    p.add_argument("--limit", type=int)

    return parser


def main(argv: list[str] | None = None) -> int:
    debug = os.environ.get("TRACKER_DEBUG") == "1"
    try:
        args = build_parser().parse_args(argv)
        settings = config.load_settings()
        if args.db:
            settings.db = Path(args.db).expanduser()
        return int(args.handler(args, settings) or 0)
    except SystemExit as exc:  # argparse (--help = 0, lỗi cú pháp = 2)
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except KeyboardInterrupt:
        _err("Đã hủy.")
        return 130
    except ValueError as exc:
        _err(str(exc))
        if debug:
            raise
        return 1
    except Exception as exc:  # noqa: BLE001
        if debug:
            raise
        _err(f"Lỗi không mong đợi ({type(exc).__name__}). Đặt TRACKER_DEBUG=1 để xem chi tiết.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
