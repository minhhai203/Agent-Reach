"""HTTP service: read API, collector ingest and the MCP Streamable HTTP endpoint.

Routes (see docs/SPEC.md):

* ``GET /healthz`` — open, ``{"ok": true}``.
* ``GET /api/students``, ``GET /api/progress?week_offset=0``, ``GET /api/sync-status`` —
  bearer ``TRACKER_API_TOKEN``.
* ``POST /ingest`` and ``GET /ingest/targets`` — bearer ``TRACKER_INGEST_TOKEN`` when it is
  set (then the API token does NOT open them), otherwise ``TRACKER_API_TOKEN``. /ingest is
  the only route that writes to the DB; /ingest/targets lists what a collector must read
  (code, platform, channel_url — no names).
* ``/mcp`` — MCP Streamable HTTP, bearer ``TRACKER_MCP_TOKEN``; mounted only when that
  token is configured (otherwise 404).

Tokens are compared with ``hmac.compare_digest`` on bytes. An empty configured token makes
the protected surface unavailable (503 for /api and /ingest, /mcp not mounted) and an empty
presented token is never valid. The API token never opens /mcp and vice versa.
Trailing-slash redirects are disabled (``/mcp/`` is a 404, never a 307 to another host).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import hmac
import ipaddress
import json
import logging
import re
import sqlite3
from collections import Counter
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import config, core, db, metrics, urls

log = logging.getLogger(__name__)

MAX_INGEST_BYTES = 256 * 1024
MAX_INGEST_VIDEOS = 200
MAX_FETCH_LIMIT = 500
MAX_WEEK_OFFSET_BACK = 520
MIN_TOKEN_LENGTH = 32
ERR_INCONSISTENT = "Số liệu gửi lên không nhất quán nên máy chủ ghi nhận là partial"
CLOCK_SKEW = dt.timedelta(minutes=5)
_COLLECTOR_RE = re.compile(r"[a-z0-9_-]{1,32}")

SECURITY_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
]

_HTTP_ERRORS = {
    400: "Yêu cầu không hợp lệ.",
    404: "Không tìm thấy đường dẫn.",
    405: "Phương thức không được hỗ trợ.",
    413: "Dữ liệu gửi lên quá lớn.",
}


class IngestError(ValueError):
    """Validation error for /ingest (message is shown to the client, in Vietnamese)."""


class IngestConflict(IngestError):
    """The batch was collected from a channel that is no longer the student's (HTTP 409)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error(message: str, status: int, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def _presented_token(authorization: str | None) -> str:
    """Extract the bearer token from an Authorization header ('' if absent/malformed)."""
    if not authorization:
        return ""
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


def token_matches(presented: str, expected: str) -> bool:
    """Constant-time comparison; empty tokens never match."""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def _auth_failure(authorization: str | None, expected: str, surface: str) -> JSONResponse | None:
    """None when authorised, else the 503/401 response to send."""
    if not expected:
        return _error(f"{surface} chưa được bật trên máy chủ (thiếu token cấu hình).", 503)
    if not token_matches(_presented_token(authorization), expected):
        return _error(
            "Thiếu hoặc sai token xác thực.", 401, headers={"WWW-Authenticate": "Bearer"}
        )
    return None


def _with_con(settings: config.Settings, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    con = db.connect(settings.db)
    try:
        return fn(con, *args, **kwargs)
    finally:
        con.close()


async def _db_call(settings: config.Settings, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    return await run_in_threadpool(functools.partial(_with_con, settings, fn, *args, **kwargs))


class _NoCommit:
    """Connection proxy whose ``with`` block does nothing.

    core functions wrap their writes in ``with con:`` (commit per call). Passing this proxy
    lets /ingest run several core calls inside ONE outer transaction on the real
    connection, so either everything is committed or nothing is.
    """

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)

    def __enter__(self) -> "_NoCommit":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------

class SecurityHeadersMiddleware:
    """Add Cache-Control: no-store and X-Content-Type-Options: nosniff to every response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in SECURITY_HEADERS if k not in names]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BearerGuard:
    """ASGI wrapper that rejects requests without the right bearer token.

    Wraps the MCP Streamable HTTP handler so unauthenticated requests are answered here
    and never reach the MCP session manager.
    """

    def __init__(self, app: ASGIApp, token: str, surface: str) -> None:
        self.app = app
        self.token = token
        self.surface = surface

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            authorization = None
            for key, value in scope.get("headers", []):
                if key == b"authorization":
                    authorization = value.decode("latin-1")
                    break
            failure = _auth_failure(authorization, self.token, self.surface)
            if failure is not None:
                await failure(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# /ingest validation
# ---------------------------------------------------------------------------

def _int_field(payload: dict, name: str, low: int, high: int) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise IngestError(f"Trường {name} phải là số nguyên.")
    if not low <= value <= high:
        raise IngestError(f"Trường {name} phải từ {low} đến {high}.")
    return value


def _str_field(payload: dict, name: str, required: bool = True) -> str:
    value = payload.get(name, None if required else "")
    if value is None and not required:
        value = ""
    if not isinstance(value, str):
        raise IngestError(f"Trường {name} phải là chuỗi.")
    return value


def _timestamp_field(payload: dict, name: str, now: dt.datetime) -> dt.datetime:
    text = _str_field(payload, name)
    try:
        moment = core.parse_timestamp(text)
    except ValueError:
        raise IngestError(f"Trường {name} phải là thời điểm ISO 8601.") from None
    if moment > now + CLOCK_SKEW:
        raise IngestError(f"Trường {name} không thể ở tương lai.")
    return moment


def validate_ingest(payload: Any, today: dt.date, now: dt.datetime) -> dict:
    """Validate an /ingest body (everything except the student lookup)."""
    if not isinstance(payload, dict):
        raise IngestError("Dữ liệu phải là một đối tượng JSON.")
    student = payload.get("student")
    if (
        isinstance(student, bool)
        or not isinstance(student, (str, int))
        or (isinstance(student, int) and not 1 <= student <= core.MAX_ID)
        or (isinstance(student, str) and not 1 <= len(student.strip()) <= 64)
    ):
        raise IngestError("Trường student phải là mã học viên (vd S01) hoặc id.")
    platform = payload.get("platform")
    if platform not in core.PLATFORMS:
        raise IngestError("Trường platform chỉ nhận tiktok hoặc youtube.")
    collector = payload.get("collector")
    if not isinstance(collector, str) or not _COLLECTOR_RE.fullmatch(collector):
        raise IngestError("Trường collector chỉ gồm a-z, 0-9, _ hoặc - (1–32 ký tự).")
    status = payload.get("status")
    if status not in core.RUN_STATUSES:
        raise IngestError("Trường status chỉ nhận ok, partial hoặc failed.")
    fetch_limit = _int_field(payload, "fetch_limit", 1, MAX_FETCH_LIMIT)
    fetched_count = _int_field(payload, "fetched_count", 0, fetch_limit * 2)
    error = _str_field(payload, "error", required=False)
    started = _timestamp_field(payload, "started_at", now)
    finished = _timestamp_field(payload, "finished_at", now)
    if finished < started:
        raise IngestError("finished_at không thể trước started_at.")
    channel_raw = payload.get("channel_url")
    if not isinstance(channel_raw, str) or not channel_raw.strip():
        raise IngestError("Thiếu trường channel_url (link kênh đã thu thập).")
    try:
        channel_url = urls.canonical_channel(platform, channel_raw)
    except ValueError as exc:
        raise IngestError(f"channel_url: {exc}") from None
    try:
        segments = core._validate_segments(payload.get("segments"), fetch_limit)
    except ValueError as exc:
        raise IngestError(f"segments: {exc}") from None
    for segment in segments:
        if any(core.parse_date(d) > today for d in segment["dates"]):
            raise IngestError("segments: ngày không thể ở tương lai.")

    videos = payload.get("videos", [])
    if not isinstance(videos, list):
        raise IngestError("Trường videos phải là danh sách.")
    if len(videos) > MAX_INGEST_VIDEOS:
        raise IngestError(f"Tối đa {MAX_INGEST_VIDEOS} video mỗi lần gửi.")
    clean: list[dict] = []
    seen: set[str] = set()
    for index, video in enumerate(videos, start=1):
        where = f"Video thứ {index}"
        if not isinstance(video, dict):
            raise IngestError(f"{where}: phải là một đối tượng.")
        key = video.get("video_key")
        url = video.get("url")
        if not isinstance(key, str) or not key.strip() or not isinstance(url, str):
            raise IngestError(f"{where}: cần video_key và url dạng chuỗi.")
        try:
            v_platform, v_key, v_url = urls.canonical_video(url)
        except ValueError as exc:
            raise IngestError(f"{where}: {exc}") from None
        if v_platform != platform:
            raise IngestError(f"{where}: link không thuộc nền tảng {platform}.")
        if v_key != key.strip():
            raise IngestError(f"{where}: video_key không khớp với link.")
        title = video.get("title", "")
        if title is None:
            title = ""
        if not isinstance(title, str):
            raise IngestError(f"{where}: title phải là chuỗi.")
        try:
            day = core.parse_date(video.get("published_date"))
        except ValueError as exc:
            raise IngestError(f"{where}: {exc}") from None
        if not isinstance(video.get("published_date"), str):
            raise IngestError(f"{where}: Ngày đăng phải theo định dạng YYYY-MM-DD.")
        if day > today:
            raise IngestError(f"{where}: Ngày đăng không thể ở tương lai.")
        countable = video.get("countable", True)
        if not isinstance(countable, bool):
            raise IngestError(f"{where}: countable phải là true hoặc false.")
        try:
            values = metrics.validate(video.get("metrics"))
        except ValueError as exc:
            raise IngestError(f"{where}: {exc}") from None
        if v_key in seen:
            continue
        seen.add(v_key)
        clean.append(
            {"video_key": v_key, "url": v_url, "title": title, "published_date": day.isoformat(),
             "countable": countable, "metrics": values}
        )

    # Never trust the client's coverage claim beyond what it actually sent.
    bound = sum(seg["fetched_count"] for seg in segments) if segments else fetched_count
    if len(clean) > bound:
        raise IngestError(
            "Số video gửi lên nhiều hơn fetched_count"
            + (" (tổng các segment)." if segments else ".")
        )
    consistent = _ingest_consistent(fetched_count, clean, segments)
    if status == "ok" and not consistent:
        status = "partial"
        error = "; ".join(x for x in (error, ERR_INCONSISTENT) if x)

    return {
        "student": student,
        "platform": platform,
        "collector": collector,
        "status": status,
        "fetch_limit": fetch_limit,
        "fetched_count": fetched_count,
        "error": error,
        "started_at": started,
        "finished_at": finished,
        "channel_url": channel_url,
        "segments": segments,
        "videos": clean,
    }


def _ingest_consistent(fetched_count: int, videos: list[dict], segments: list[dict]) -> bool:
    """True when the counts claimed by the collector match the videos actually sent.

    Without segments: fetched_count == number of videos. With segments: every segment's
    dates are exactly its fetched_count and are all backed by sent videos, the run-level
    fetched_count is the largest segment, and every sent video belongs to some segment.
    """
    if not segments:
        return fetched_count == len(videos)
    sent = Counter(v["published_date"] for v in videos)
    union: Counter = Counter()
    for segment in segments:
        dates = Counter(segment["dates"])
        if len(segment["dates"]) != segment["fetched_count"]:
            return False
        if any(sent[day] < n for day, n in dates.items()):
            return False
        union += dates
    if fetched_count != max(seg["fetched_count"] for seg in segments):
        return False
    return all(union[day] >= n for day, n in sent.items())


def _store_ingest(con: sqlite3.Connection, payload: Any) -> dict:
    """Validate and write one ingest batch in a single transaction."""
    now = config.now_vn()
    data = validate_ingest(payload, now.date(), now)
    try:
        student = core.get_student(con, data["student"])
    except ValueError as exc:
        raise IngestError(str(exc)) from None
    if not student["active"]:
        raise IngestError("Học viên đã ngừng theo dõi.")
    if student[f"{data['platform']}_url"] != data["channel_url"]:
        raise IngestConflict(
            "Link kênh đã thu thập không còn là kênh hiện tại của học viên trên "
            f"{data['platform']} (giáo viên vừa đổi hoặc xóa kênh). Hãy chạy lại collect-push "
            "để lấy danh sách kênh mới."
        )
    status, error = data["status"], data["error"]
    proxy = _NoCommit(con)
    with con:  # one transaction: commit on success, roll back on any error
        upserted = core.upsert_observed_videos(
            proxy, student["id"], data["platform"], data["videos"], "observed"
        )
        if upserted["foreign"]:
            if status == "ok":
                status = "partial"
            error = "; ".join(x for x in (error, core.foreign_error(len(upserted["foreign"]))) if x)
        run_id = core.record_sync_run(
            proxy,
            student["id"],
            data["platform"],
            f"ingest:{data['collector']}",
            status,
            data["started_at"],
            data["finished_at"],
            data["fetch_limit"],
            data["fetched_count"],
            [v["published_date"] for v in data["videos"]],
            error,
            channel_url=data["channel_url"],
            segments=data["segments"],
        )
        metrics.record_snapshots(proxy, student["id"], data["platform"], data["videos"],
                                 data["finished_at"], run_id)
    return {"ok": True, "added": upserted["added"], "run_id": run_id, "status": status}


def ingest_targets(con: sqlite3.Connection) -> list[dict]:
    """What a remote collector must read: active students' channels, without names."""
    out = []
    for student in core.list_students(con):
        for platform in core.PLATFORMS:
            channel = student[f"{platform}_url"]
            if channel:
                out.append({"code": student["code"], "platform": platform, "channel_url": channel})
    return out


async def _read_body_limited(request: Request, limit: int) -> bytes | None:
    """Read the body, giving up (None) as soon as it exceeds ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def build_app(settings: config.Settings) -> Starlette:
    """Build the Starlette app (MCP at /mcp only when settings.mcp_token is set)."""

    def api_guard(request: Request) -> JSONResponse | None:
        return _auth_failure(request.headers.get("authorization"), settings.api_token, "API")

    def ingest_guard(request: Request) -> JSONResponse | None:
        # A dedicated ingest token, when set, is the ONLY credential for /ingest*.
        expected = settings.ingest_token or settings.api_token
        return _auth_failure(request.headers.get("authorization"), expected, "Ingest")

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def students(request: Request) -> JSONResponse:
        if (failure := api_guard(request)) is not None:
            return failure
        return JSONResponse(await _db_call(settings, core.list_students))

    async def progress(request: Request) -> JSONResponse:
        if (failure := api_guard(request)) is not None:
            return failure
        raw = request.query_params.get("week_offset", "0").strip()
        if not re.fullmatch(r"[+-]?\d{1,4}", raw):
            return _error("week_offset phải là số nguyên (0 = tuần này, -1 = tuần trước).", 400)
        offset = int(raw)
        if offset > 0:
            return _error("Không thể xem tiến độ của tuần trong tương lai.", 400)
        if offset < -MAX_WEEK_OFFSET_BACK:
            return _error(f"week_offset không được nhỏ hơn -{MAX_WEEK_OFFSET_BACK}.", 400)
        try:
            data = await _db_call(settings, core.week_progress, offset, settings=settings)
        except ValueError as exc:
            return _error(str(exc), 400)
        return JSONResponse(data)

    async def sync_status(request: Request) -> JSONResponse:
        if (failure := api_guard(request)) is not None:
            return failure
        return JSONResponse(await _db_call(settings, core.sync_status))

    async def video_metrics(request: Request) -> JSONResponse:
        if (failure := api_guard(request)) is not None:
            return failure
        student = request.query_params.get("student", "").strip()
        period = request.query_params.get("period", "day")
        count = request.query_params.get("count", "7")
        offset = request.query_params.get("offset", "0")
        if not student or not re.fullmatch(r"\d{1,2}", count) or not re.fullmatch(r"\d{1,4}", offset):
            return _error("Cần student, count từ 1 đến 52 và offset từ 0 đến 5200.", 400)
        try:
            data = await _db_call(settings, metrics.history, student, period, int(count), int(offset))
        except ValueError as exc:
            return _error(str(exc), 400)
        return JSONResponse(data)

    async def ingest_targets_route(request: Request) -> JSONResponse:
        if (failure := ingest_guard(request)) is not None:
            return failure
        return JSONResponse(await _db_call(settings, ingest_targets))

    async def ingest(request: Request) -> JSONResponse:
        if (failure := ingest_guard(request)) is not None:
            return failure
        body = await _read_body_limited(request, MAX_INGEST_BYTES)
        if body is None:
            return _error("Dữ liệu gửi lên quá lớn (tối đa 256 KB).", 413)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return _error("Dữ liệu không phải JSON hợp lệ.", 400)
        if not isinstance(payload, dict):
            return _error("Dữ liệu phải là một đối tượng JSON.", 400)
        try:
            result = await _db_call(settings, _store_ingest, payload)
        except IngestConflict as exc:
            return _error(str(exc), 409)
        except IngestError as exc:
            return _error(str(exc), 400)
        except RecursionError:
            return _error("Dữ liệu lồng nhau quá sâu.", 400)
        except ValueError as exc:  # core validation (Vietnamese message)
            return _error(str(exc), 400)
        return JSONResponse(result)

    routes: list[Route] = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/api/students", students, methods=["GET"]),
        Route("/api/progress", progress, methods=["GET"]),
        Route("/api/sync-status", sync_status, methods=["GET"]),
        Route("/api/metrics", video_metrics, methods=["GET"]),
        Route("/ingest", ingest, methods=["POST"]),
        Route("/ingest/targets", ingest_targets_route, methods=["GET"]),
    ]

    mcp = None
    if settings.mcp_token:
        from .mcp_server import build_mcp

        mcp = build_mcp(settings)
        mcp.streamable_http_app()  # creates mcp.session_manager (lazy in the SDK)
        from mcp.server.fastmcp.server import StreamableHTTPASGIApp

        handler = StreamableHTTPASGIApp(mcp.session_manager)
        # An ASGI-app endpoint (not a function) accepts every method; auth runs first.
        routes.append(Route("/mcp", BearerGuard(handler, settings.mcp_token, "MCP")))

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        if mcp is None:
            yield
        else:
            # The SDK session manager needs its task group running; run() is single-use.
            async with mcp.session_manager.run():
                yield

    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        message = _HTTP_ERRORS.get(exc.status_code, "Có lỗi xảy ra.")
        return _error(message, exc.status_code, headers=getattr(exc, "headers", None))

    async def server_error(request: Request, exc: Exception) -> JSONResponse:
        log.error("Lỗi máy chủ khi xử lý %s %s: %s", request.method, request.url.path,
                  type(exc).__name__)
        return _error("Lỗi máy chủ.", 500)

    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        exception_handlers={HTTPException: http_error, Exception: server_error},
    )
    # Never answer "/x/" with a 307 built from the client's Host header.
    app.router.redirect_slashes = False
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _is_loopback(host: str) -> bool:
    text = str(host or "").strip().strip("[]")
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def check_bind(settings: config.Settings) -> None:
    """Refuse to expose a non-loopback bind with neither token configured."""
    if (
        not _is_loopback(settings.host)
        and not settings.api_token
        and not settings.mcp_token
        and not settings.ingest_token
    ):
        raise ValueError(
            f"Từ chối mở cổng trên {settings.host}: chưa đặt TRACKER_API_TOKEN, "
            "TRACKER_MCP_TOKEN hoặc TRACKER_INGEST_TOKEN. Hãy đặt token, hoặc bind 127.0.0.1."
        )


def check_tokens(settings: config.Settings) -> None:
    """Refuse to serve with a configured token shorter than MIN_TOKEN_LENGTH characters.

    The message names the environment variable, never the value.
    """
    for env_name, value in (
        ("TRACKER_API_TOKEN", settings.api_token),
        ("TRACKER_MCP_TOKEN", settings.mcp_token),
        ("TRACKER_INGEST_TOKEN", settings.ingest_token),
    ):
        if value and len(value) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"{env_name} quá ngắn: cần ít nhất {MIN_TOKEN_LENGTH} ký tự. Sinh token bằng: "
                "python3 -c \"import secrets;print(secrets.token_urlsafe(32))\""
            )


def serve(settings: config.Settings, with_scheduler: bool = False) -> None:
    """Run the HTTP service with uvicorn on settings.host:settings.port."""
    import uvicorn

    check_bind(settings)
    check_tokens(settings)
    app = build_app(settings)
    scheduler = None
    if with_scheduler:
        from .scheduler import Scheduler

        scheduler = Scheduler(settings)
        scheduler.start()
    try:
        uvicorn.run(app, host=settings.host, port=int(settings.port), server_header=False,
                    log_level="info")
    finally:
        if scheduler is not None:
            scheduler.stop()
