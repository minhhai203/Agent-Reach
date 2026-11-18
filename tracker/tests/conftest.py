"""Shared fixtures: temporary DB, open connection and a frozen VN clock."""

from __future__ import annotations

import datetime as dt
from typing import Callable

import pytest

from video_tracker import config, db


@pytest.fixture
def settings(tmp_path) -> config.Settings:
    """Settings pointing at a fresh temporary database."""
    return config.Settings(db=tmp_path / "data" / "tracker.sqlite3")


@pytest.fixture
def con(settings):
    """Open connection to the temporary database."""
    connection = db.connect(settings.db)
    yield connection
    connection.close()


def vn(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> dt.datetime:
    """Build a VN-aware datetime."""
    return dt.datetime(year, month, day, hour, minute, tzinfo=config.TZ)


@pytest.fixture
def freeze_time(monkeypatch) -> Callable[[dt.datetime], dt.datetime]:
    """Return a function that freezes `config.now_vn()` at the given moment."""

    def _freeze(moment: dt.datetime) -> dt.datetime:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=config.TZ)
        moment = moment.astimezone(config.TZ)
        monkeypatch.setattr(config, "now_vn", lambda: moment)
        return moment

    return _freeze
