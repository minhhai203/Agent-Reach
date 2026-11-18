"""SQLite connection and schema migrations (PRAGMA user_version)."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

SQL_V1 = """
CREATE TABLE students(
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    weekly_goal INTEGER NOT NULL CHECK(weekly_goal BETWEEN 1 AND 50),
    tiktok_url TEXT NOT NULL DEFAULT '',
    youtube_url TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE videos(
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id),
    platform TEXT NOT NULL CHECK(platform IN ('tiktok','youtube')),
    video_key TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    published_date TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('manual','observed','api','reach')),
    created_at TEXT NOT NULL,
    UNIQUE(platform, video_key)
);
CREATE INDEX idx_videos_student_date ON videos(student_id, published_date);
CREATE TABLE sync_runs(
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id),
    platform TEXT NOT NULL,
    collector TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok','partial','failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    fetch_limit INTEGER NOT NULL,
    fetched_count INTEGER NOT NULL,
    dates_json TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_sync_runs_student_platform ON sync_runs(student_id, platform, id);
CREATE TABLE reports(
    id INTEGER PRIMARY KEY,
    week_start TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL,
    UNIQUE(week_start, kind)
);
"""

# v2: countable=0 marks a post seen on the channel (dated) that could not be read as a
# video (possibly a TikTok photo post); such rows never count toward the weekly goal.
SQL_V2 = """
ALTER TABLE videos ADD COLUMN countable INTEGER NOT NULL DEFAULT 1;
"""

# v3: a sync run remembers WHICH channel it read (coverage only trusts runs of the student's
# current channel) and per-tab segments (YouTube /videos and /shorts are judged separately);
# reports get an atomic delivery claim so two callers never deliver the same week twice.
SQL_V3 = """
ALTER TABLE sync_runs ADD COLUMN channel_url TEXT NOT NULL DEFAULT '';
ALTER TABLE sync_runs ADD COLUMN segments_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE reports ADD COLUMN state TEXT NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN claimed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN claim_id TEXT NOT NULL DEFAULT '';
"""

SQL_V4 = """
CREATE TABLE video_metric_snapshots(
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id),
    sync_run_id INTEGER REFERENCES sync_runs(id) ON DELETE SET NULL,
    observed_at TEXT NOT NULL,
    view_count INTEGER CHECK(view_count IS NULL OR view_count >= 0),
    like_count INTEGER CHECK(like_count IS NULL OR like_count >= 0),
    comment_count INTEGER CHECK(comment_count IS NULL OR comment_count >= 0),
    UNIQUE(video_id, observed_at)
);
CREATE INDEX idx_metrics_video_time ON video_metric_snapshots(video_id, observed_at);
"""

MIGRATIONS: list[str] = [SQL_V1, SQL_V2, SQL_V3, SQL_V4]


def _statements(script: str) -> list[str]:
    """Split a migration script into complete SQL statements."""
    out: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                out.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        out.append(buffer.strip())
    return out


def migrate(con: sqlite3.Connection) -> int:
    """Apply pending migrations atomically; return the resulting schema version.

    Safe when several connections open a fresh database at the same time: the check and
    the DDL run inside ONE ``BEGIN IMMEDIATE`` transaction (write lock taken up front) and
    the version is re-read after the lock is held, so only one connection migrates and the
    others see the finished schema.
    """
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version >= len(MIGRATIONS):
        return version
    if con.in_transaction:
        con.commit()
    previous = con.isolation_level
    con.isolation_level = None  # manual transaction control
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            for index in range(version, len(MIGRATIONS)):
                for statement in _statements(MIGRATIONS[index]):
                    con.execute(statement)
                con.execute(f"PRAGMA user_version = {index + 1}")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    finally:
        con.isolation_level = previous
    return con.execute("PRAGMA user_version").fetchone()[0]


def _enable_wal(con: sqlite3.Connection, timeout: float = 10.0) -> None:
    """Switch to WAL. Changing the journal mode does not use the busy handler, so when
    several connections open a fresh file at once it is retried here (bounded)."""
    deadline = time.monotonic() + timeout
    delay = 0.01
    while True:
        try:
            if str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                return
            con.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def connect(path: str | os.PathLike) -> sqlite3.Connection:
    """Open the tracker database, creating it (dir 0700, file 0600) and migrating.

    The connection is closed again if any set-up step (pragmas, migration) fails.
    """
    is_memory = str(path) == ":memory:"
    if not is_memory:
        db_path = Path(path).expanduser()
        if not db_path.parent.exists():
            db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not db_path.exists():
            # Create the file with restrictive permissions before SQLite opens it.
            fd = os.open(db_path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass
        target = str(db_path)
    else:
        target = ":memory:"
    con = sqlite3.connect(target, timeout=10)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 10000")
        con.execute("PRAGMA foreign_keys = ON")
        if not is_memory:
            _enable_wal(con)
        migrate(con)
    except BaseException:
        con.close()
        raise
    return con
