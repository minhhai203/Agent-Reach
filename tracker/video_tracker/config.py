"""Configuration read from environment variables, plus the VN clock."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

DEFAULT_DB = Path.home() / ".video-tracker" / "tracker.sqlite3"


def now_vn() -> dt.datetime:
    """Current time in Asia/Ho_Chi_Minh (tz-aware). Tests monkeypatch this."""
    return dt.datetime.now(TZ)


@dataclass
class Settings:
    """Runtime settings. Secrets are excluded from repr."""

    db: Path = DEFAULT_DB
    sync_limit: int = 30
    fresh_hours: float = 36
    api_token: str = field(default="", repr=False)
    mcp_token: str = field(default="", repr=False)
    ingest_token: str = field(default="", repr=False)
    allowed_hosts: list[str] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8765
    sync_every_hours: float = 6
    report_at: str = "mon 08:00"
    ai: bool = False
    openai_api_key: str = field(default="", repr=False)
    openai_model: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    deliver: str = "stdout"
    deliver_file: str = ""
    deliver_webhook_url: str = field(default="", repr=False)
    deliver_webhook_token: str = field(default="", repr=False)
    deliver_command: str = ""
    zalo_mod_url: str = "http://127.0.0.1:18792/api/action"
    zalo_mod_token: str = field(default="", repr=False)
    zalo_target_id: str = field(default="", repr=False)
    zalo_target_type: str = "group"
    youtube_api_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        self.db = Path(self.db).expanduser()


# Environment variables a child process (yt-dlp, delivery command) may inherit. Everything
# else — tokens, API keys, OPENAI_API_KEY … — is withheld.
CHILD_ENV_KEYS = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "XDG_CACHE_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
)


def child_env(environ: dict | None = None) -> dict[str, str]:
    """Minimal environment for subprocesses: only CHILD_ENV_KEYS that are present."""
    env = os.environ if environ is None else environ
    return {key: str(env[key]) for key in CHILD_ENV_KEYS if key in env}


def secret_values(settings: "Settings") -> list[str]:
    """Every non-empty secret value in Settings (all fields declared with repr=False)."""
    values = []
    for item in fields(settings):
        if item.repr:
            continue
        value = getattr(settings, item.name, "")
        if isinstance(value, str) and value.strip():
            values.append(value)
    # Longest first so a secret containing another is masked whole.
    return sorted(set(values), key=len, reverse=True)


def _env(environ: dict, name: str, default: str = "") -> str:
    return str(environ.get(name, default)).strip()


def _env_int(environ: dict, name: str, default: int) -> int:
    raw = _env(environ, name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Biến môi trường {name} phải là số nguyên.") from None


def _env_float(environ: dict, name: str, default: float) -> float:
    raw = _env(environ, name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Biến môi trường {name} phải là số.") from None


def load_settings(environ: dict | None = None) -> Settings:
    """Build Settings from the environment (read fresh on every call)."""
    env = os.environ if environ is None else environ
    hosts = [h.strip() for h in _env(env, "TRACKER_ALLOWED_HOSTS").split(",") if h.strip()]
    return Settings(
        db=Path(_env(env, "TRACKER_DB") or DEFAULT_DB),
        sync_limit=_env_int(env, "TRACKER_SYNC_LIMIT", 30),
        fresh_hours=_env_float(env, "TRACKER_FRESH_HOURS", 36),
        api_token=_env(env, "TRACKER_API_TOKEN"),
        mcp_token=_env(env, "TRACKER_MCP_TOKEN"),
        ingest_token=_env(env, "TRACKER_INGEST_TOKEN"),
        allowed_hosts=hosts,
        host=_env(env, "TRACKER_HOST") or "127.0.0.1",
        port=_env_int(env, "TRACKER_PORT", 8765),
        sync_every_hours=_env_float(env, "TRACKER_SYNC_EVERY_HOURS", 6),
        # Explicitly empty TRACKER_REPORT_AT disables reports; unset uses the default.
        report_at=_env(env, "TRACKER_REPORT_AT", "mon 08:00"),
        ai=_env(env, "TRACKER_AI") == "1",
        openai_api_key=_env(env, "OPENAI_API_KEY"),
        openai_model=_env(env, "TRACKER_OPENAI_MODEL"),
        openai_base_url=_env(env, "OPENAI_BASE_URL") or "https://api.openai.com/v1",
        deliver=_env(env, "TRACKER_DELIVER") or "stdout",
        deliver_file=_env(env, "TRACKER_DELIVER_FILE"),
        deliver_webhook_url=_env(env, "TRACKER_DELIVER_WEBHOOK_URL"),
        deliver_webhook_token=_env(env, "TRACKER_DELIVER_WEBHOOK_TOKEN"),
        deliver_command=_env(env, "TRACKER_DELIVER_COMMAND"),
        zalo_mod_url=_env(env, "TRACKER_ZALO_MOD_URL") or "http://127.0.0.1:18792/api/action",
        zalo_mod_token=_env(env, "TRACKER_ZALO_MOD_TOKEN"),
        zalo_target_id=_env(env, "TRACKER_ZALO_TARGET_ID"),
        zalo_target_type=_env(env, "TRACKER_ZALO_TARGET_TYPE") or "group",
        youtube_api_key=_env(env, "YOUTUBE_API_KEY"),
    )
