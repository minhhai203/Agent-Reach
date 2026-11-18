"""Weekly report: deterministic Vietnamese text, optional AI commentary, delivery."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid

import httpx

from . import config, core
from .deliver import deliver

MAX_CHARS = 1800  # limit of ONE chat message; longer reports are split into parts
FORBIDDEN_PHRASES = ("chưa đăng", "không đăng")
CLAIM_TTL = dt.timedelta(minutes=10)
_MARKER_RESERVE = 12  # room for the "(i/n) " prefix

AI_ACTIONS = {
    "check_channels": "Kiểm tra lại kênh đã khai báo và trạng thái đồng bộ.",
    "offer_support": "Chủ động hỏi học viên có cần hỗ trợ làm video không.",
    "share_resources": "Chia sẻ tài nguyên và hướng dẫn làm video cho nhóm.",
}

_GROUPS = (
    ("met", "✅ Đủ mục tiêu"),
    ("at_least", "🟡 Ít nhất (có thể còn video chưa đọc tới)"),
    ("below_goal", "🔴 Thiếu theo dữ liệu hiện có"),
    ("insufficient_data", "⚪ Chưa đủ dữ liệu để kết luận"),
)

_PLATFORM_NAMES = {"tiktok": "TikTok", "youtube": "YouTube"}
_REASON_TEXT = {
    "never_synced": "chưa đồng bộ lần nào",
    "last_sync_failed": "lần đồng bộ gần nhất lỗi",
    "stale": "dữ liệu đã cũ",
}

SYSTEM_PROMPT = (
    "Chọn tối đa ba hành động hỗ trợ giảng viên từ các mã: "
    "check_channels, offer_support, share_resources. "
    'Chỉ trả JSON dạng {"actions":["check_channels"]}, không thêm trường hoặc văn bản. '
    "Không kết luận về học viên hay suy đoán nguyên nhân."
)


def _reason_text(reason: str) -> str:
    if reason == "no_channel":
        return "chưa khai báo kênh"
    platform, _, key = reason.partition(":")
    name = _PLATFORM_NAMES.get(platform, platform)
    return f"{name}: {_REASON_TEXT.get(key, key)}"


def unreadable_suffix(student: dict) -> str:
    """' (+N bài đăng không đọc được, có thể là bài ảnh)' or '' when there are none."""
    n = int(student.get("unreadable_posts") or 0)
    return f" (+{n} bài đăng không đọc được, có thể là bài ảnh)" if n > 0 else ""


def _student_line(student: dict) -> str:
    line = f"• {student['name']} {student['count']}/{student['weekly_goal']}"
    if student["status"] == "insufficient_data" and student.get("reasons"):
        line += " (" + "; ".join(_reason_text(r) for r in student["reasons"]) + ")"
    return line + unreadable_suffix(student)


def _fmt_day(value: str) -> str:
    year, month, day = value.split("-")
    return f"{day}/{month}"


def _blocks(progress: dict, commentary: str = "") -> list[tuple[str, list[str]]]:
    """Report as ordered blocks ``(continuation_title, lines)``; title '' = not repeatable."""
    header = (
        f"📊 Báo cáo đăng video tuần {_fmt_day(progress['week_start'])}–"
        f"{_fmt_day(progress['week_end'])} (giờ VN)"
    )
    students = progress["students"]
    blocks: list[tuple[str, list[str]]] = [("", [header])]
    if not students:
        blocks.append(("", ["Chưa có học viên nào đang theo dõi."]))
    for status, title in _GROUPS:
        members = [s for s in students if s["status"] == status]
        if members:
            blocks.append((f"{title} (tiếp)", [f"{title} ({len(members)})"]
                           + [_student_line(s) for s in members]))
    if commentary:
        blocks.append(("💬 Nhận xét (AI) (tiếp):",
                       ["💬 Nhận xét (AI):"] + commentary.splitlines()))
    blocks.append(("", [progress.get("disclaimer") or core.DISCLAIMER]))
    return blocks


def render_text(progress: dict, commentary: str = "") -> str:
    """Deterministic full report text (never truncated; see render_parts for chat sizes)."""
    return "\n\n".join("\n".join(lines) for _, lines in _blocks(progress, commentary))


def _hard_split(line: str, limit: int) -> list[str]:
    """Split one over-long line at whitespace (or hard) into pieces of at most `limit`."""
    pieces = []
    while len(line) > limit:
        cut = line.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        pieces.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    pieces.append(line)
    return pieces


def render_parts(progress: dict, commentary: str = "", limit: int = MAX_CHARS) -> list[str]:
    """Split the report into chat messages of at most `limit` characters each.

    Splits only between groups or between lines; the header is in part 1, the disclaimer
    in the last part; a group cut in two repeats its title with "(tiếp)". With more than
    one part each part starts with "(i/n) ".
    """
    budget = limit - _MARKER_RESERVE
    parts: list[str] = []
    current = ""
    for cont_title, lines in _blocks(progress, commentary):
        first = True
        for raw in lines:
            for line in _hard_split(raw, budget):
                sep = "" if not current else ("\n\n" if first else "\n")
                if current and len(current) + len(sep) + len(line) > budget:
                    parts.append(current)
                    current = (cont_title + "\n" + line) if (cont_title and not first) else line
                    if len(current) > budget:
                        current = line
                else:
                    current += sep + line
                first = False
    if current:
        parts.append(current)
    if len(parts) > 1:
        total = len(parts)
        parts = [f"({i}/{total}) {part}" for i, part in enumerate(parts, start=1)]
    return parts


def anonymized_payload(progress: dict) -> dict:
    """Data safe to send to an AI provider: no names, URLs or titles (counts only)."""
    return {
        "week_start": progress["week_start"],
        "week_end": progress["week_end"],
        "summary": dict(progress["summary"]),
        "students": [
            {
                "code": s["code"],
                "weekly_goal": s["weekly_goal"],
                "count": s["count"],
                "unreadable_posts": int(s.get("unreadable_posts") or 0),
                "status": s["status"],
                "coverage": {p: v.get("coverage", "") for p, v in s["platforms"].items()},
                "by_source": dict(s["by_source"]),
            }
            for s in progress["students"]
        ],
    }


def ai_commentary(
    payload: dict, settings: config.Settings, http_client: httpx.Client | None = None
) -> str:
    """Ask an OpenAI-compatible chat endpoint for a short commentary (codes only)."""
    url = settings.openai_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.openai_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    try:
        if http_client is not None:
            response = http_client.post(url, json=body, headers=headers, timeout=60)
        else:
            with httpx.Client(timeout=60) as own:
                response = own.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        raise RuntimeError("Gọi AI quá thời gian chờ.") from None
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Không gọi được AI ({exc.__class__.__name__}).") from None
    if response.status_code != 200:
        raise RuntimeError(f"AI trả HTTP {response.status_code}.")
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        raise RuntimeError("AI trả về dữ liệu không đúng định dạng.") from None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("AI trả về nội dung rỗng.")
    return content.strip()


def filter_commentary(text: str, students: list[dict]) -> tuple[str, str]:
    """Render only fixed action templates selected by a strictly validated AI response."""
    try:
        response = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        response = None
    if not isinstance(response, dict) or set(response) != {"actions"}:
        return "", "Bỏ nhận xét AI: phản hồi không đúng định dạng hành động."
    actions = response["actions"]
    if (not isinstance(actions, list) or not 1 <= len(actions) <= len(AI_ACTIONS)
            or any(not isinstance(action, str) or action not in AI_ACTIONS for action in actions)
            or len(set(actions)) != len(actions)):
        return "", "Bỏ nhận xét AI: danh sách hành động không hợp lệ."
    return "\n".join(AI_ACTIONS[action] for action in actions), ""


def build_report(
    con: sqlite3.Connection,
    settings: config.Settings,
    week_offset: int = -1,
    use_ai: bool | None = None,
    http_client: httpx.Client | None = None,
) -> dict:
    progress = core.week_progress(con, week_offset, settings)
    wanted = settings.ai if use_ai is None else bool(use_ai)
    ai_used = False
    ai_note = ""
    commentary = ""
    if wanted:
        if not settings.openai_api_key:
            ai_note = "Bỏ qua nhận xét AI: chưa cấu hình OPENAI_API_KEY."
        elif not settings.openai_model:
            ai_note = "Bỏ qua nhận xét AI: chưa cấu hình TRACKER_OPENAI_MODEL."
        else:
            try:
                raw = ai_commentary(anonymized_payload(progress), settings, http_client)
            except RuntimeError as exc:
                ai_note = f"Bỏ qua nhận xét AI: {exc}"
            else:
                kept, ai_note = filter_commentary(raw, progress["students"])
                if kept:
                    commentary = kept
                    ai_used = True
    return {
        "week_start": progress["week_start"],
        "week_end": progress["week_end"],
        "text": render_text(progress, commentary),
        "parts": render_parts(progress, commentary),
        "ai_used": ai_used,
        "ai_note": ai_note,
        "progress": progress,
    }


def _claim(con: sqlite3.Connection, week_start: str, force: bool, claim_id: str) -> str:
    """Atomically claim the (week_start, 'weekly') row before delivering.

    Returns '' when claimed, else the Vietnamese reason to skip. Runs in BEGIN IMMEDIATE,
    so two callers can never both see "not delivered, not claimed" and both deliver.
    A claim younger than CLAIM_TTL held by someone else blocks (even with force);
    an older one is considered abandoned and is taken over.
    """
    now = config.now_vn()
    if con.in_transaction:
        con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            "SELECT delivered, state, claimed_at, claim_id FROM reports"
            " WHERE week_start = ? AND kind = 'weekly'",
            (week_start,),
        ).fetchone()
        if row is not None:
            if row["state"] == "sending" and row["claim_id"] != claim_id and row["claimed_at"]:
                try:
                    claimed = core.parse_timestamp(row["claimed_at"])
                except ValueError:
                    claimed = None
                if claimed is not None and now - claimed < CLAIM_TTL:
                    con.rollback()
                    return ("Một tiến trình khác đang gửi báo cáo tuần này (nhận việc lúc "
                            f"{row['claimed_at']}); bỏ qua để tránh gửi trùng.")
            if row["delivered"] and not force:
                con.rollback()
                return "Báo cáo tuần này đã được gửi; dùng force để gửi lại."
            con.execute(
                "UPDATE reports SET state = 'sending', claimed_at = ?, claim_id = ?"
                " WHERE week_start = ? AND kind = 'weekly'",
                (now.isoformat(timespec="seconds"), claim_id, week_start),
            )
        else:
            con.execute(
                "INSERT INTO reports (week_start, kind, created_at, delivered, body, state,"
                " claimed_at, claim_id) VALUES (?, 'weekly', ?, 0, '', 'sending', ?, ?)",
                (week_start, now.isoformat(timespec="seconds"),
                 now.isoformat(timespec="seconds"), claim_id),
            )
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return ""


def _release(con: sqlite3.Connection, week_start: str, claim_id: str, ok: bool,
             body: str | None) -> None:
    """Finish a claim: delivered=1 only on success (never reset), claim released."""
    if con.in_transaction:
        con.commit()
    assignments = "state = ?, claimed_at = '', claim_id = ''"
    params: list = ["sent" if ok else "failed"]
    if ok:
        assignments += ", delivered = 1"
    if body is not None:
        assignments += ", body = ?, created_at = ?"
        params += [body, config.now_vn().isoformat(timespec="seconds")]
    con.execute(
        f"UPDATE reports SET {assignments} WHERE week_start = ? AND kind = 'weekly'"
        " AND claim_id = ?",
        (*params, week_start, claim_id),
    )
    con.commit()


def send_report(
    con: sqlite3.Connection,
    settings: config.Settings,
    week_offset: int = -1,
    force: bool = False,
    http_client: httpx.Client | None = None,
) -> dict:
    """Build and deliver the weekly report once per week (retry-safe, `force` overrides).

    The week's row is claimed atomically before delivery, so concurrent callers (CLI and
    scheduler, two schedulers …) never deliver the same week twice. A failed delivery
    releases the claim so a later run can retry.
    """
    week_start = core.week_bounds(week_offset)[0].isoformat()
    claim_id = uuid.uuid4().hex
    reason = _claim(con, week_start, force, claim_id)
    if reason:
        delivered = con.execute(
            "SELECT delivered FROM reports WHERE week_start = ? AND kind = 'weekly'",
            (week_start,),
        ).fetchone()
        return {
            "skipped": True,
            "week_start": week_start,
            "delivered": bool(delivered and delivered["delivered"]),
            "reason": reason,
        }
    ok = False
    body = None
    try:
        report = build_report(con, settings, week_offset, http_client=http_client)
        body = report["text"]
        result = deliver(report["text"], settings, http_client=http_client,
                         parts=report["parts"])
        ok = bool(result.get("ok"))
    finally:
        _release(con, week_start, claim_id, ok, body)
    return {
        "skipped": False,
        "week_start": week_start,
        "week_end": report["week_end"],
        "delivered": ok,
        "deliver": result,
        "ai_used": report["ai_used"],
        "ai_note": report["ai_note"],
        "text": report["text"],
        "parts": report["parts"],
    }
