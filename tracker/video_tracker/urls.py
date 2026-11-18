"""Validation and canonicalisation of TikTok / YouTube URLs (ported from legacy)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit, SplitResult

HOSTS = {
    "youtube.com": "youtube",
    "www.youtube.com": "youtube",
    "m.youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "www.tiktok.com": "tiktok",
    "m.tiktok.com": "tiktok",
    "vm.tiktok.com": "tiktok",
    "vt.tiktok.com": "tiktok",
}

# Hosts that only redirect somewhere else; never accepted as channel links.
REDIRECT_HOSTS = {"youtu.be", "vm.tiktok.com", "vt.tiktok.com"}
TIKTOK_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}

MAX_URL_LENGTH = 2048

_TIKTOK_HANDLE = r"@[A-Za-z0-9._-]{1,64}"
_YOUTUBE_HANDLE = r"@[\w.-]{1,100}"
_YOUTUBE_CHANNEL_ID = r"UC[A-Za-z0-9_-]{22}"
_YOUTUBE_CUSTOM = r"[\w.-]{1,100}"
_YOUTUBE_TABS = r"(?:/(?:videos|shorts|streams|featured|live))?"
_YOUTUBE_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

ERR_URL = "Chỉ chấp nhận liên kết HTTPS TikTok hoặc YouTube hợp lệ."
ERR_TOO_LONG = "Liên kết quá dài."


def _sane_name(name: str) -> bool:
    """A handle/custom name must contain a letter or digit, must not be '.'/'..' and must
    not start or end with a dot (so '@..', '/c/..', '@.x' and '@x.' are refused)."""
    bare = name[1:] if name.startswith("@") else name
    return (
        any(ch.isalnum() for ch in bare)
        and bare not in (".", "..")
        and not bare.startswith(".")
        and not bare.endswith(".")
    )


def _split(url: str) -> SplitResult:
    text = str(url or "").strip()
    if not text:
        raise ValueError(ERR_URL)
    if len(text) > MAX_URL_LENGTH:
        raise ValueError(ERR_TOO_LONG)
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError(ERR_URL) from None
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or port not in (None, 443)
        or parts.hostname not in HOSTS
    ):
        raise ValueError(ERR_URL)
    return parts


def platform_of(url: str) -> str:
    """Return 'tiktok' or 'youtube' for an allowed HTTPS URL; ValueError otherwise."""
    return HOSTS[_split(url).hostname]


def canonical_channel(platform: str, url: str) -> str:
    """Return the canonical channel URL for `platform`; reject video links/redirectors."""
    parts = _split(url)
    actual = HOSTS[parts.hostname]
    if platform not in ("tiktok", "youtube"):
        raise ValueError("Nền tảng không hợp lệ (chỉ tiktok hoặc youtube).")
    if actual != platform:
        raise ValueError("Liên kết kênh không đúng nền tảng.")
    if parts.hostname in REDIRECT_HOSTS:
        raise ValueError("Hãy nhập link kênh đầy đủ, không dùng link rút gọn.")
    if parts.query or parts.fragment:
        raise ValueError("Link kênh không được chứa tham số (?…) hoặc #….")
    path = parts.path
    if platform == "tiktok":
        match = re.fullmatch(rf"/({_TIKTOK_HANDLE})/?", path)
        if not match or not _sane_name(match.group(1)):
            raise ValueError("Link kênh TikTok phải có dạng https://www.tiktok.com/@ten_kenh.")
        return f"https://www.tiktok.com/{match.group(1)}"
    for pattern, prefix in (
        (_YOUTUBE_HANDLE, ""),
        (_YOUTUBE_CHANNEL_ID, "channel/"),
        (_YOUTUBE_CUSTOM, "c/"),
    ):
        match = re.fullmatch(rf"/{prefix}({pattern}){_YOUTUBE_TABS}/?", path)
        if match and _sane_name(match.group(1)):
            return f"https://www.youtube.com/{prefix}{match.group(1)}"
    raise ValueError(
        "Link kênh YouTube phải có dạng https://www.youtube.com/@ten_kenh, /channel/UC… hoặc /c/ten."
    )


def canonical_video(url: str) -> tuple[str, str, str]:
    """Return (platform, video_key, canonical_url) for a video link."""
    parts = _split(url)
    platform = HOSTS[parts.hostname]
    if platform == "youtube":
        video_id = ""
        segments = parts.path.split("/")
        if parts.hostname == "youtu.be":
            video_id = parts.path.strip("/")
        elif parts.path in ("/watch", "/watch/"):
            video_id = parse_qs(parts.query).get("v", [""])[0]
        elif parts.path.startswith(("/shorts/", "/live/")) and len(segments) > 2:
            video_id = segments[2]
            if any(segments[3:]):
                video_id = ""
        if not _YOUTUBE_VIDEO_ID.fullmatch(video_id):
            raise ValueError("Hãy nhập link video YouTube đầy đủ.")
        return "youtube", video_id, f"https://www.youtube.com/watch?v={video_id}"

    if parts.hostname in TIKTOK_SHORT_HOSTS:
        short = re.fullmatch(r"/([A-Za-z0-9]{4,32})/?", parts.path)
        if short:
            code = short.group(1)
            return "tiktok", f"short:{code}", f"https://{parts.hostname}/{code}/"
    else:
        match = re.fullmatch(rf"/({_TIKTOK_HANDLE})/video/(\d{{10,25}})/?", parts.path)
        if match and _sane_name(match.group(1)):
            handle, video_id = match.groups()
            return "tiktok", video_id, f"https://www.tiktok.com/{handle}/video/{video_id}"
    raise ValueError("Hãy nhập link video TikTok hoặc link chia sẻ vm.tiktok.com / vt.tiktok.com.")
