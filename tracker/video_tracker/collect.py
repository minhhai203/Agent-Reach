"""Collectors: read recent videos of a channel (yt-dlp or YouTube Data API) and store them.

`collect_channel` never touches the DB. `sync_student` / `sync_all` collect, upsert
videos and always record a sync_run (including failed ones) and never raise for
collector failures.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import config, core, metrics, urls

PLATFORMS = ("tiktok", "youtube")
YTDLP_TIMEOUT = 180
HTTP_TIMEOUT = 30
YT_API = "https://www.googleapis.com/youtube/v3"

ERR_NO_YTDLP = "Chưa cài yt-dlp"
ERR_BOT = "Nền tảng yêu cầu xác minh (chặn theo IP)"
ERR_RATE = "Bị giới hạn tần suất"
ERR_TIMEOUT = "Hết thời gian chờ khi đọc kênh"
ERR_NO_VIDEO = "Không đọc được video nào từ kênh"
ERR_PARTIAL_DATE = "Một số video không có ngày đăng nên bị bỏ qua"
ERR_UNEXPECTED = "Lỗi không mong đợi khi đồng bộ"

_YT_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_TT_ID = re.compile(r"\d{10,25}")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)[?#]\S*")
_TAB_MISSING_MARKERS = ("does not have a", "404", "not found", "no videos")
# Per-item yt-dlp error for one TikTok post, e.g. "ERROR: [TikTok] 7704…: No video formats found!"
_TT_ITEM_ERROR = re.compile(r"^ERROR:\s*\[tiktok\]\s*(\d+)\s*:", re.IGNORECASE)
# A TikTok post id encodes its creation time: id >> 32 is the Unix timestamp (seconds).
_TT_MIN_EPOCH = int(dt.datetime(2016, 1, 1, tzinfo=dt.timezone.utc).timestamp())


@dataclass
class CollectResult:
    platform: str
    collector: str
    status: str  # ok | partial | failed
    fetch_limit: int
    fetched_count: int
    videos: list[dict] = field(default_factory=list)
    error: str = ""
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    # Canonical channel URL actually read ('' when the URL was invalid).
    channel_url: str = ""
    # Per-tab results [{"name","fetched_count","dates"}]; YouTube via yt-dlp has one entry
    # per existing tab (videos, shorts). Empty → the run-level numbers describe the channel.
    segments: list[dict] = field(default_factory=list)


@dataclass
class _Proc:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _clean_error(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove secrets and URL query strings from a message destined for the DB."""
    out = str(text or "")
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    out = _URL_QUERY.sub(r"\1", out)
    return out.strip()[:300]


def _vn_date_from_epoch(value: Any) -> str | None:
    try:
        moment = dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return moment.astimezone(config.TZ).date().isoformat()


def _vn_date_from_iso(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(config.TZ).date().isoformat()


def _date_from_entry(entry: dict) -> str | None:
    if entry.get("timestamp") not in (None, ""):
        day = _vn_date_from_epoch(entry.get("timestamp"))
        if day:
            return day
    upload = str(entry.get("upload_date") or "").strip()
    if re.fullmatch(r"\d{8}", upload):
        try:
            return dt.datetime.strptime(upload, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    return None


def _tiktok_id_date(video_id: str) -> str | None:
    """VN date encoded in a TikTok post id (`id >> 32` = epoch seconds); None if implausible."""
    if not _TT_ID.fullmatch(str(video_id or "")):
        return None
    seconds = int(video_id) >> 32
    if seconds < _TT_MIN_EPOCH or seconds > config.now_vn().timestamp():
        return None
    return _vn_date_from_epoch(seconds)


def _today_iso() -> str:
    return config.now_vn().date().isoformat()


def _video_url(platform: str, handle: str, video_id: str) -> str:
    if platform == "youtube":
        return f"https://www.youtube.com/watch?v={video_id}"
    return f"https://www.tiktok.com/{handle}/video/{video_id}"


def _handle_of(channel_url: str) -> str:
    path = urlsplit(channel_url).path.strip("/")
    return path.split("/")[0]


def _finish(result: CollectResult) -> CollectResult:
    result.finished_at = config.now_vn()
    return result


# ---------------------------------------------------------------------------
# yt-dlp collector
# ---------------------------------------------------------------------------

def _run(argv: list[str], timeout: float) -> _Proc:
    """Run a command (list argv, no shell). Tests monkeypatch this."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                              env=config.child_env())
    except subprocess.TimeoutExpired as exc:
        def _text(value: Any) -> str:
            if isinstance(value, bytes):
                return value.decode("utf-8", "replace")
            return value or ""
        return _Proc(-1, _text(exc.stdout), _text(exc.stderr), timed_out=True)
    return _Proc(proc.returncode, proc.stdout or "", proc.stderr or "")


def _ytdlp_argv() -> list[str] | None:
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-m", "yt_dlp"]


def _map_stderr(stderr: str, returncode: int) -> str:
    low = (stderr or "").lower()
    if "not a bot" in low or "sign in to confirm" in low:
        return ERR_BOT
    if "429" in low or "too many requests" in low:
        return ERR_RATE
    return f"yt-dlp báo lỗi khi đọc kênh (mã thoát {returncode})"


def _parse_entries(stdout: str) -> list[dict]:
    entries = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            entries.append(data)
    return entries


@dataclass
class _Tab:
    name: str = ""
    videos: list[dict] = field(default_factory=list)  # valid, dated, not future
    dated: int = 0  # videos with a valid date (future ones included)
    undated: int = 0
    lines: int = 0
    returncode: int = 0
    error: str = ""
    missing: bool = False
    timed_out: bool = False


def _read_tab(platform: str, handle: str, argv_base: list[str], tab_url: str,
              limit: int, deadline: float, name: str = "") -> _Tab:
    argv = argv_base + [
        "--ignore-config", "--no-warnings", "--skip-download", "--dump-json",
        "--playlist-end", str(limit), "--socket-timeout", "15", "--", tab_url,
    ]
    remaining = max(1.0, deadline - time.monotonic())
    proc = _run(argv, min(YTDLP_TIMEOUT, remaining))
    tab = _Tab(name=name, returncode=proc.returncode, timed_out=proc.timed_out)
    entries = _parse_entries(proc.stdout)
    tab.lines = len(entries)
    today = _today_iso()
    parsed_ids: set[str] = set()
    for entry in entries:
        vid = str(entry.get("id") or "").strip()
        valid_id = bool(_YT_ID.fullmatch(vid)) if platform == "youtube" else bool(_TT_ID.fullmatch(vid))
        day = _date_from_entry(entry)
        if valid_id and not day and platform == "tiktok":
            day = _tiktok_id_date(vid)
        if valid_id:
            parsed_ids.add(vid)
        if not valid_id or not day:
            tab.undated += 1
            continue
        tab.dated += 1
        if day > today:
            continue
        tab.videos.append({
            "video_key": vid,
            "url": _video_url(platform, handle, vid),
            "title": str(entry.get("title") or "").strip(),
            "published_date": day,
            "metrics": metrics.from_collector(entry),
        })
    item_errors_resolved = False
    if platform == "tiktok" and not proc.timed_out and proc.returncode != 0:
        item_errors_resolved = _add_unreadable_posts(tab, handle, proc.stderr, parsed_ids, today)
    if proc.timed_out:
        tab.error = ERR_TIMEOUT
    elif proc.returncode != 0 and not item_errors_resolved:
        low = (proc.stderr or "").lower()
        tab.error = _map_stderr(proc.stderr, proc.returncode)
        tab.missing = tab.lines == 0 and tab.error.startswith("yt-dlp") and any(
            m in low for m in _TAB_MISSING_MARKERS
        )
    return tab


def _add_unreadable_posts(tab: _Tab, handle: str, stderr: str, parsed_ids: set[str],
                         today: str) -> bool:
    """Turn per-item TikTok errors into dated, non-countable posts.

    Returns True when there is at least one error line and EVERY error line is a per-item
    TikTok error whose post is accounted for (read as a video, or dated from its id), i.e.
    nothing about the channel listing is unknown.
    """
    error_lines = [ln.strip() for ln in (stderr or "").splitlines()
                   if ln.strip().upper().startswith("ERROR:")]
    resolved = bool(error_lines)
    seen: set[str] = set()
    for line in error_lines:
        match = _TT_ITEM_ERROR.match(line)
        vid = match.group(1) if match else ""
        if not vid or not _TT_ID.fullmatch(vid):
            resolved = False
            continue
        if vid in parsed_ids or vid in seen:
            continue
        day = _tiktok_id_date(vid)
        if not day:
            resolved = False
            continue
        seen.add(vid)
        tab.dated += 1
        if day > today:
            continue
        tab.videos.append({
            "video_key": vid,
            "url": _video_url("tiktok", handle, vid),
            "title": "",
            "published_date": day,
            "countable": False,
        })
    return resolved


def _collect_ytdlp(platform: str, channel: str, limit: int, result: CollectResult) -> CollectResult:
    base = _ytdlp_argv()
    if base is None:
        result.status, result.error = "failed", ERR_NO_YTDLP
        return _finish(result)
    handle = _handle_of(channel)
    if platform == "tiktok":
        tab_specs = [("", channel)]
    else:
        tab_specs = [("videos", f"{channel}/videos"), ("shorts", f"{channel}/shorts")]
    deadline = time.monotonic() + YTDLP_TIMEOUT
    tabs = [_read_tab(platform, handle, base, u, limit, deadline, name) for name, u in tab_specs]
    if platform == "youtube":
        # Coverage judges each tab on its own: a full /shorts tab that never reached the
        # week's Monday must not be hidden by a short /videos tab. A missing tab is absent.
        result.segments = [
            {"name": t.name, "fetched_count": t.dated,
             "dates": [v["published_date"] for v in t.videos]}
            for t in tabs if not t.missing
        ]

    merged: dict[str, dict] = {}
    for tab in tabs:
        for video in tab.videos:
            existing = merged.setdefault(video["video_key"], video)
            if existing is not video and video.get("metrics"):
                current = existing.setdefault("metrics", {})
                for name, value in video["metrics"].items():
                    if current.get(name) is None:
                        current[name] = value
    videos = list(merged.values())
    result.videos = videos
    result.fetched_count = max((t.dated for t in tabs), default=0)

    any_lines = any(t.lines for t in tabs)
    if not videos and not any_lines:
        real = [t for t in tabs if not t.missing and t.error]
        result.status = "failed"
        result.error = real[0].error if real else ERR_NO_VIDEO
        return _finish(result)

    problems = []
    if any(t.undated for t in tabs):
        problems.append(ERR_PARTIAL_DATE)
    for tab in tabs:
        if tab.error and not (tab.missing and videos):
            problems.append(tab.error)
    result.status = "partial" if problems else "ok"
    result.error = "; ".join(dict.fromkeys(problems))
    return _finish(result)


# ---------------------------------------------------------------------------
# YouTube Data API collector
# ---------------------------------------------------------------------------

class _ApiError(Exception):
    """Collector error with a message that is already safe to store."""


def _api_get(client: httpx.Client, path: str, params: dict, key: str) -> dict:
    try:
        resp = client.get(f"{YT_API}/{path}", params={**params, "key": key}, timeout=HTTP_TIMEOUT)
    except httpx.TimeoutException:
        raise _ApiError(ERR_TIMEOUT) from None
    except httpx.HTTPError:
        raise _ApiError("Không kết nối được YouTube Data API") from None
    if resp.status_code == 429:
        raise _ApiError(ERR_RATE)
    if resp.status_code in (400, 401, 403):
        raise _ApiError("YouTube Data API từ chối yêu cầu (khóa không hợp lệ hoặc hết hạn mức)")
    if resp.status_code >= 400:
        raise _ApiError(f"YouTube Data API trả lỗi HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise _ApiError("YouTube Data API trả dữ liệu không hợp lệ") from None
    if not isinstance(data, dict):
        raise _ApiError("YouTube Data API trả dữ liệu không hợp lệ")
    return data


def _collect_api(channel: str, limit: int, key: str, client: httpx.Client | None,
                 result: CollectResult) -> CollectResult:
    own = client is None
    http = client or httpx.Client()
    try:
        path = urlsplit(channel).path
        if path.startswith("/@"):
            lookup = {"forHandle": path[1:]}
        elif path.startswith("/channel/"):
            lookup = {"id": path[len("/channel/"):]}
        else:
            raise _ApiError("Không tra được kênh dạng /c/… bằng YouTube Data API; "
                            "hãy dùng link @tên_kênh hoặc /channel/UC…")
        data = _api_get(http, "channels", {"part": "contentDetails", **lookup}, key)
        items = data.get("items") or []
        try:
            uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        except (IndexError, KeyError, TypeError):
            raise _ApiError("Không tìm thấy kênh YouTube này") from None

        today = _today_iso()
        videos: list[dict] = []
        seen: set[str] = set()
        undated = 0
        dated = 0
        page_token = ""
        raw_seen = 0
        while raw_seen < limit:
            params = {"part": "contentDetails,snippet", "playlistId": uploads,
                      "maxResults": min(limit - raw_seen, 50)}
            if page_token:
                params["pageToken"] = page_token
            page = _api_get(http, "playlistItems", params, key)
            for item in page.get("items") or []:
                raw_seen += 1
                details = item.get("contentDetails") or {}
                vid = str(details.get("videoId") or "").strip()
                day = _vn_date_from_iso(details.get("videoPublishedAt"))
                if not _YT_ID.fullmatch(vid) or not day:
                    undated += 1
                    continue
                dated += 1
                if day > today or vid in seen:
                    continue
                seen.add(vid)
                title = str((item.get("snippet") or {}).get("title") or "").strip()
                videos.append({"video_key": vid, "url": _video_url("youtube", "", vid),
                               "title": title, "published_date": day})
            page_token = page.get("nextPageToken") or ""
            if not page_token:
                break
        result.videos = videos
        result.fetched_count = dated
        result.status = "partial" if undated else "ok"
        result.error = ERR_PARTIAL_DATE if undated else ""
    except _ApiError as exc:
        result.status, result.error = "failed", str(exc)
        result.videos, result.fetched_count = [], 0
    finally:
        if own:
            http.close()
    return _finish(result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_channel(platform: str, channel_url: str, limit: int,
                    settings: config.Settings, http_client: httpx.Client | None = None) -> CollectResult:
    """Read up to `limit` recent videos of a channel. Does not touch the DB; never raises."""
    key = settings.youtube_api_key if platform == "youtube" else ""
    collector = "youtube-api" if key else "yt-dlp"
    result = CollectResult(platform=platform, collector=collector, status="failed",
                           fetch_limit=int(limit), fetched_count=0, started_at=config.now_vn())
    secrets = tuple(config.secret_values(settings))
    try:
        channel = urls.canonical_channel(platform, channel_url)
        result.channel_url = channel
        if key:
            _collect_api(channel, int(limit), key, http_client, result)
        else:
            _collect_ytdlp(platform, channel, int(limit), result)
    except ValueError as exc:
        result.status, result.error = "failed", str(exc)
        result.videos, result.fetched_count = [], 0
        _finish(result)
    except Exception:  # noqa: BLE001 - collectors must never raise
        result.status, result.error = "failed", f"{ERR_UNEXPECTED} {platform}"
        result.videos, result.fetched_count = [], 0
        _finish(result)
    result.error = _clean_error(result.error, secrets)
    if result.status == "failed":
        result.videos, result.fetched_count, result.segments = [], 0, []
    return result


def _source_for(collector: str) -> str:
    return "api" if collector == "youtube-api" else "observed"


def sync_student(con, student: dict, settings: config.Settings,
                 platforms: list[str] | None = None,
                 http_client: httpx.Client | None = None) -> list[dict]:
    """Collect every channel the student has, upsert videos and record one sync_run each."""
    wanted = [p for p in PLATFORMS if platforms is None or p in platforms]
    limit = int(settings.sync_limit)
    out: list[dict] = []
    for platform in wanted:
        channel = (student.get(f"{platform}_url") or "").strip()
        if not channel:
            continue
        secrets = tuple(config.secret_values(settings))
        started = config.now_vn()
        added = 0
        # The run is tied to the channel URL read NOW: if the instructor changes the URL
        # meanwhile, this run no longer counts as evidence for the new channel.
        try:
            channel_key = urls.canonical_channel(platform, channel)
        except ValueError:
            channel_key = channel
        try:
            res = collect_channel(platform, channel, limit, settings, http_client=http_client)
            status, error = res.status, res.error
            if res.videos:
                upserted = core.upsert_observed_videos(
                    con, student["id"], platform, res.videos, _source_for(res.collector))
                added = upserted["added"]
                if upserted["foreign"]:
                    # Those videos are not counted for this student → never "complete".
                    status = "partial"
                    error = "; ".join(
                        x for x in (error, core.foreign_error(len(upserted["foreign"]))) if x)
            run_id = core.record_sync_run(
                con, student["id"], platform, res.collector, status,
                res.started_at or started, res.finished_at or config.now_vn(),
                res.fetch_limit, res.fetched_count,
                [v["published_date"] for v in res.videos], error,
                channel_url=res.channel_url or channel_key, segments=res.segments)
            metrics.record_snapshots(con, student["id"], platform, res.videos,
                                     res.finished_at or config.now_vn(), run_id)
            out.append({"platform": platform, "status": status, "added": added,
                        "fetched_count": res.fetched_count, "error": error})
        except Exception:  # noqa: BLE001 - never raise for collector failures
            error = _clean_error(f"{ERR_UNEXPECTED} {platform}", secrets)
            collector = "youtube-api" if platform == "youtube" and settings.youtube_api_key else "yt-dlp"
            try:
                core.record_sync_run(con, student["id"], platform, collector, "failed",
                                     started, config.now_vn(), limit, 0, [], error,
                                     channel_url=channel_key)
            except Exception:  # noqa: BLE001
                error += " (không ghi được lịch sử đồng bộ)"
            out.append({"platform": platform, "status": "failed", "added": 0,
                        "fetched_count": 0, "error": error})
    return out


def sync_all(con, settings: config.Settings, platforms: list[str] | None = None,
             http_client: httpx.Client | None = None) -> dict:
    """Sync every active student; return a per-student summary plus totals."""
    students_out = []
    totals = {"runs": 0, "ok": 0, "partial": 0, "failed": 0, "added": 0}
    for student in core.list_students(con):
        results = sync_student(con, student, settings, platforms, http_client=http_client)
        for r in results:
            totals["runs"] += 1
            totals[r["status"]] += 1
            totals["added"] += r["added"]
        students_out.append({"code": student["code"], "name": student["name"], "results": results})
    return {"students": students_out, "totals": totals}
