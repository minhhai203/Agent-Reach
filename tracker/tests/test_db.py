import os
import sqlite3
import stat

import pytest

from video_tracker import db


def test_connect_creates_dirs_and_schema(settings):
    con = db.connect(settings.db)
    try:
        assert settings.db.exists()
        assert stat.S_IMODE(os.stat(settings.db).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(settings.db.parent).st_mode) == 0o700
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"students", "videos", "sync_runs", "reports"} <= tables
        assert con.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert isinstance(con.execute("SELECT 1 AS x").fetchone(), sqlite3.Row)
    finally:
        con.close()


def test_connect_is_idempotent(settings):
    db.connect(settings.db).close()
    con = db.connect(settings.db)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    finally:
        con.close()


def test_constraints(con):
    con.execute(
        "INSERT INTO students(code, name, weekly_goal, created_at) VALUES ('S01','A',3,'x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO students(code, name, weekly_goal, created_at) VALUES ('S02','B',0,'x')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # foreign key
        con.execute(
            "INSERT INTO videos(student_id, platform, video_key, url, published_date, source,"
            " created_at) VALUES (99,'tiktok','1','u','2026-01-01','manual','x')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # platform check
        con.execute(
            "INSERT INTO videos(student_id, platform, video_key, url, published_date, source,"
            " created_at) VALUES (1,'vimeo','1','u','2026-01-01','manual','x')"
        )


def test_memory_database():
    con = db.connect(":memory:")
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    finally:
        con.close()


def test_v1_database_upgrades_in_place(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("BEGIN;\n" + db.SQL_V1 + "\nPRAGMA user_version = 1;\nCOMMIT;")
    old.execute("INSERT INTO students(code, name, weekly_goal, created_at) VALUES ('S01','A',3,'x')")
    old.execute(
        "INSERT INTO videos(student_id, platform, video_key, url, published_date, source,"
        " created_at) VALUES (1,'tiktok','7000000000000000001','u','2026-11-10','observed','x')"
    )
    old.commit()
    old.close()

    con = db.connect(path)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4 == len(db.MIGRATIONS)
        rows = con.execute("SELECT video_key, countable FROM videos").fetchall()
        assert [(r["video_key"], r["countable"]) for r in rows] == [("7000000000000000001", 1)]
        assert con.execute("SELECT name FROM students").fetchone()["name"] == "A"
        con.execute(
            "INSERT INTO videos(student_id, platform, video_key, url, published_date, source,"
            " created_at, countable) VALUES (1,'tiktok','2','u','2026-11-10','observed','x',0)"
        )
    finally:
        con.close()
    # Re-opening does not re-run v2 (ALTER would fail on the existing column).
    db.connect(path).close()
