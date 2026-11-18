"""HTTP API: auth matrix, read endpoints, /ingest validation and atomicity."""

from __future__ import annotations

import copy
import json

import pytest
from starlette.testclient import TestClient

from conftest import vn
from video_tracker import config, core, db, http_api

API_TOKEN = "api-token-for-tests-123"
MCP_TOKEN = "mcp-token-for-tests-456"
YT_ID = "dQw4w9WgXcQ"
YT_ID2 = "abcdefghijk"
NOW = vn(2026, 11, 18, 10, 0)  # Wednesday; week 2026-11-16 .. 2026-11-22

API_ROUTES = [
    ("GET", "/api/students"),
    ("GET", "/api/progress"),
    ("GET", "/api/sync-status"),
    ("POST", "/ingest"),
]


@pytest.fixture
def api_settings(tmp_path) -> config.Settings:
    return config.Settings(
        db=tmp_path / "data" / "tracker.sqlite3",
        api_token=API_TOKEN,
        mcp_token=MCP_TOKEN,
        allowed_hosts=["testserver"],
    )


@pytest.fixture
def frozen(freeze_time):
    return freeze_time(NOW)


@pytest.fixture
def client(api_settings, frozen):
    with TestClient(http_api.build_app(api_settings)) as test_client:
        yield test_client


@pytest.fixture
def student(api_settings):
    con = db.connect(api_settings.db)
    try:
        return core.add_student(con, "Lan", 3, youtube_url="https://www.youtube.com/@lan")
    finally:
        con.close()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def counts(settings) -> tuple[int, int]:
    con = db.connect(settings.db)
    try:
        videos = con.execute("SELECT count(*) FROM videos").fetchone()[0]
        runs = con.execute("SELECT count(*) FROM sync_runs").fetchone()[0]
        return videos, runs
    finally:
        con.close()


def payload(**overrides) -> dict:
    body = {
        "student": "S01",
        "platform": "youtube",
        "collector": "mac",
        "status": "ok",
        "fetch_limit": 30,
        "fetched_count": 2,
        "error": "",
        "started_at": "2026-11-18T09:00:00+07:00",
        "finished_at": "2026-11-18T09:01:00+07:00",
        "channel_url": "https://www.youtube.com/@lan",
        "videos": [
            {"video_key": YT_ID, "url": f"https://www.youtube.com/watch?v={YT_ID}",
             "title": "Bài 1", "published_date": "2026-11-17"},
            {"video_key": YT_ID2, "url": f"https://youtu.be/{YT_ID2}",
             "title": "Bài 2", "published_date": "2026-11-10"},
        ],
    }
    body.update(overrides)
    return body


def call(client, method, path, headers=None, body=None):
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, headers=headers, json=body if body is not None else payload())


def assert_security_headers(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


# ---------------------------------------------------------------------------
# Auth matrix
# ---------------------------------------------------------------------------

def test_healthz_is_open(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert_security_headers(response)


@pytest.mark.parametrize("method,path", API_ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Bearer {API_TOKEN}x"},
        {"Authorization": f"Basic {API_TOKEN}"},
        {"Authorization": API_TOKEN},
        {"Authorization": f"Bearer {MCP_TOKEN}"},  # MCP token must not open the API
    ],
    ids=["none", "empty", "no-value", "wrong", "longer", "basic", "no-scheme", "mcp-token"],
)
def test_api_routes_reject_bad_tokens(client, api_settings, student, method, path, headers):
    response = call(client, method, path, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "error" in response.json()
    assert_security_headers(response)
    assert API_TOKEN not in response.text and MCP_TOKEN not in response.text
    assert counts(api_settings) == (0, 0)


@pytest.mark.parametrize("method,path", API_ROUTES)
def test_api_routes_accept_api_token(client, student, method, path):
    response = call(client, method, path, headers=auth(API_TOKEN))
    assert response.status_code == 200, response.text
    assert_security_headers(response)


@pytest.mark.parametrize("method,path", API_ROUTES)
@pytest.mark.parametrize("token", ["", "anything", MCP_TOKEN])
def test_api_unavailable_without_configured_token(tmp_path, frozen, method, path, token):
    settings = config.Settings(db=tmp_path / "t.sqlite3", api_token="", mcp_token=MCP_TOKEN,
                               allowed_hosts=["testserver"])
    with TestClient(http_api.build_app(settings)) as client:
        response = call(client, method, path, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 503
        assert "error" in response.json()
        assert client.get("/healthz").status_code == 200


def test_bearer_scheme_is_case_insensitive(client):
    response = client.get("/api/students", headers={"Authorization": f"bearer {API_TOKEN}"})
    assert response.status_code == 200


def test_token_matches_rejects_empty():
    assert not http_api.token_matches("", "")
    assert not http_api.token_matches("", "x")
    assert not http_api.token_matches("x", "")
    assert http_api.token_matches("tôken", "tôken")


def test_mcp_rejects_api_token_and_api_rejects_mcp_token(client):
    headers = {"Accept": "application/json, text/event-stream"}
    response = client.post("/mcp", json={}, headers={**headers, **auth(API_TOKEN)})
    assert response.status_code == 401
    response = client.get("/api/students", headers=auth(MCP_TOKEN))
    assert response.status_code == 401


def test_mcp_not_mounted_without_token(tmp_path, frozen):
    settings = config.Settings(db=tmp_path / "t.sqlite3", api_token=API_TOKEN, mcp_token="")
    with TestClient(http_api.build_app(settings)) as client:
        for token in ("", API_TOKEN, "x"):
            response = client.post("/mcp", json={}, headers=auth(token))
            assert response.status_code == 404
            assert response.json() == {"error": "Không tìm thấy đường dẫn."}


def test_unknown_route_and_method_are_json(client):
    response = client.get("/nope")
    assert response.status_code == 404 and "error" in response.json()
    assert_security_headers(response)
    response = client.get("/ingest", headers=auth(API_TOKEN))
    assert response.status_code == 405 and "error" in response.json()


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------

def test_students_and_sync_status(client, student):
    response = client.get("/api/students", headers=auth(API_TOKEN))
    assert [s["code"] for s in response.json()] == ["S01"]
    response = client.get("/api/sync-status", headers=auth(API_TOKEN))
    rows = response.json()
    assert rows[0]["platform"] == "youtube" and rows[0]["synced"] is False


def test_progress_default_and_previous_week(client, student):
    response = client.get("/api/progress", headers=auth(API_TOKEN))
    data = response.json()
    assert data["week_start"] == "2026-11-16" and data["is_current"] is True
    assert data["students"][0]["status"] == "insufficient_data"
    response = client.get("/api/progress?week_offset=-1", headers=auth(API_TOKEN))
    assert response.json()["week_start"] == "2026-11-09"


@pytest.mark.parametrize("value", ["1", "+2", "abc", "1.5", "", "-99999", "-521", "0x1"])
def test_progress_rejects_bad_week_offset(client, value):
    response = client.get(f"/api/progress?week_offset={value}", headers=auth(API_TOKEN))
    assert response.status_code == 400
    assert response.json()["error"]


def test_progress_positive_offset_message(client):
    response = client.get("/api/progress?week_offset=1", headers=auth(API_TOKEN))
    assert response.status_code == 400
    assert response.json() == {"error": "Không thể xem tiến độ của tuần trong tương lai."}


# ---------------------------------------------------------------------------
# /ingest
# ---------------------------------------------------------------------------

def test_ingest_happy_path(client, api_settings, student):
    response = client.post("/ingest", json=payload(), headers=auth(API_TOKEN))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["added"] == 2 and isinstance(body["run_id"], int)
    assert_security_headers(response)

    con = db.connect(api_settings.db)
    try:
        videos = con.execute("SELECT * FROM videos ORDER BY video_key").fetchall()
        assert {v["source"] for v in videos} == {"observed"}
        assert {v["url"] for v in videos} == {
            f"https://www.youtube.com/watch?v={YT_ID}",
            f"https://www.youtube.com/watch?v={YT_ID2}",
        }
        run = con.execute("SELECT * FROM sync_runs WHERE id = ?", (body["run_id"],)).fetchone()
        assert run["collector"] == "ingest:mac"
        assert run["status"] == "ok" and run["fetch_limit"] == 30 and run["fetched_count"] == 2
        assert sorted(json.loads(run["dates_json"])) == ["2026-11-10", "2026-11-17"]
        assert run["finished_at"] == "2026-11-18T09:01:00+07:00"
    finally:
        con.close()

    # Re-sending the same videos adds nothing but records another run.
    again = client.post("/ingest", json=payload(), headers=auth(API_TOKEN)).json()
    assert again["added"] == 0 and again["run_id"] != body["run_id"]
    assert counts(api_settings) == (2, 2)


def test_ingest_by_numeric_id_and_tiktok(client, api_settings, student):
    con = db.connect(api_settings.db)
    try:
        core.update_student(con, student["id"], tiktok_url="https://www.tiktok.com/@lan")
    finally:
        con.close()
    body = payload(
        student=student["id"],
        platform="tiktok",
        channel_url="https://www.tiktok.com/@lan",
        fetched_count=1,
        videos=[{"video_key": "7300000000000000001",
                 "url": "https://www.tiktok.com/@lan/video/7300000000000000001",
                 "published_date": "2026-11-16"}],
    )
    response = client.post("/ingest", json=body, headers=auth(API_TOKEN))
    assert response.status_code == 200, response.text
    assert response.json()["added"] == 1


def test_ingest_empty_videos_failed_run(client, api_settings, student):
    body = payload(status="failed", fetched_count=0, videos=[], error="Bị giới hạn tần suất")
    response = client.post("/ingest", json=body, headers=auth(API_TOKEN))
    assert response.status_code == 200
    assert counts(api_settings) == (0, 1)


def _many_videos(n):
    return [
        {"video_key": f"v{i:010d}", "url": f"https://www.youtube.com/watch?v=v{i:010d}",
         "published_date": "2026-11-17"}
        for i in range(n)
    ]


def _second_video(**changes):
    videos = copy.deepcopy(payload()["videos"])
    videos[1].update(changes)
    return {"videos": videos}


BAD_PAYLOADS = {
    "unknown-student": {"student": "S99"},
    "student-bool": {"student": True},
    "student-empty": {"student": ""},
    "student-list": {"student": ["S01"]},
    "platform": {"platform": "facebook"},
    "collector-upper": {"collector": "Mac"},
    "collector-empty": {"collector": ""},
    "collector-long": {"collector": "a" * 33},
    "collector-space": {"collector": "my mac"},
    "collector-colon": {"collector": "x:y"},
    "status": {"status": "done"},
    "fetch-limit-zero": {"fetch_limit": 0},
    "fetch-limit-big": {"fetch_limit": 501},
    "fetch-limit-str": {"fetch_limit": "30"},
    "fetch-limit-bool": {"fetch_limit": True},
    "fetch-limit-float": {"fetch_limit": 30.0},
    "fetched-negative": {"fetched_count": -1},
    "fetched-too-many": {"fetched_count": 61},
    "error-not-str": {"error": 5},
    "started-bad": {"started_at": "yesterday"},
    "started-missing": {"started_at": None},
    "finished-future": {"finished_at": "2026-11-19T10:00:00+07:00"},
    "finished-before-started": {"finished_at": "2026-11-18T08:00:00+07:00"},
    "videos-not-list": {"videos": {"a": 1}},
    "too-many-videos": {"videos": _many_videos(201)},
    "video-not-object": {"videos": ["https://youtu.be/dQw4w9WgXcQ"]},
    "video-wrong-platform": _second_video(
        url="https://www.tiktok.com/@lan/video/7300000000000000001",
        video_key="7300000000000000001"),
    "video-key-mismatch": _second_video(video_key="zzzzzzzzzzz"),
    "video-http": _second_video(url=f"http://www.youtube.com/watch?v={YT_ID2}"),
    "video-other-host": _second_video(url=f"https://evil.example/watch?v={YT_ID2}"),
    "video-channel-url": _second_video(url="https://www.youtube.com/@lan"),
    "video-missing-key": _second_video(video_key=""),
    "video-future": _second_video(published_date="2026-11-19"),
    "video-bad-date": _second_video(published_date="17/11/2026"),
    "video-date-int": _second_video(published_date=20261117),
    "video-title-int": _second_video(title=5),
}


@pytest.mark.parametrize("changes", list(BAD_PAYLOADS.values()), ids=list(BAD_PAYLOADS))
def test_ingest_validation_errors_write_nothing(client, api_settings, student, changes):
    response = client.post("/ingest", json=payload(**changes), headers=auth(API_TOKEN))
    assert response.status_code == 400, response.text
    message = response.json()["error"]
    assert isinstance(message, str) and message
    assert_security_headers(response)
    assert counts(api_settings) == (0, 0)


def test_ingest_inactive_student(client, api_settings, student):
    con = db.connect(api_settings.db)
    try:
        core.update_student(con, student["id"], active=False)
    finally:
        con.close()
    response = client.post("/ingest", json=payload(), headers=auth(API_TOKEN))
    assert response.status_code == 400
    assert response.json() == {"error": "Học viên đã ngừng theo dõi."}
    assert counts(api_settings) == (0, 0)


def test_ingest_200_videos_is_allowed(client, api_settings, student):
    body = payload(videos=_many_videos(200), fetch_limit=200, fetched_count=200)
    response = client.post("/ingest", json=body, headers=auth(API_TOKEN))
    assert response.status_code == 200, response.text
    assert response.json()["added"] == 200


@pytest.mark.parametrize(
    "raw", [b"[1, 2]", b"\"text\"", b"42", b"null", b"{not json", b"\xff\xfe{}"],
    ids=["list", "string", "number", "null", "broken", "not-utf8"],
)
def test_ingest_requires_json_object(client, api_settings, student, raw):
    response = client.post("/ingest", content=raw, headers={
        **auth(API_TOKEN), "Content-Type": "application/json"})
    assert response.status_code == 400
    assert "error" in response.json()
    assert counts(api_settings) == (0, 0)


def test_ingest_is_atomic_when_a_write_fails(api_settings, frozen, student, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(core, "record_sync_run", boom)
    with TestClient(http_api.build_app(api_settings), raise_server_exceptions=False) as client:
        response = client.post("/ingest", json=payload(), headers=auth(API_TOKEN))
    assert response.status_code == 500
    assert response.json() == {"error": "Lỗi máy chủ."}
    assert "disk full" not in response.text
    assert counts(api_settings) == (0, 0)


def test_ingest_oversize_with_content_length(client, api_settings, student):
    body = json.dumps(payload(error="x" * (http_api.MAX_INGEST_BYTES + 10))).encode()
    response = client.post("/ingest", content=body, headers={
        **auth(API_TOKEN), "Content-Type": "application/json"})
    assert response.status_code == 413
    assert "error" in response.json()
    assert counts(api_settings) == (0, 0)


def test_ingest_oversize_streamed_without_content_length(client, api_settings, student):
    chunk = b" " * 65536

    def body():
        yield b'{"student": "S01", "pad": "'
        for _ in range(5):  # 320 KB, no Content-Length header (chunked)
            yield chunk
        yield b'"}'

    response = client.post("/ingest", content=body(), headers={
        **auth(API_TOKEN), "Content-Type": "application/json"})
    assert response.status_code == 413
    assert counts(api_settings) == (0, 0)


def test_ingest_lying_content_length_is_still_limited(api_settings, frozen, student):
    """A small declared Content-Length does not let a bigger body through."""
    app = http_api.build_app(api_settings)
    import anyio

    big = b"{" + b" " * (http_api.MAX_INGEST_BYTES + 100) + b"}"
    messages = [
        {"type": "http.request", "body": big[:200_000], "more_body": True},
        {"type": "http.request", "body": big[200_000:], "more_body": False},
    ]
    sent: list[dict] = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/ingest", "raw_path": b"/ingest", "query_string": b"",
        "root_path": "", "server": ("testserver", 80), "client": ("127.0.0.1", 1),
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", f"Bearer {API_TOKEN}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", b"10"),
        ],
    }
    anyio.run(app, scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
    assert counts(api_settings) == (0, 0)


# ---------------------------------------------------------------------------
# Binding safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"])
def test_check_bind_allows_loopback_without_tokens(tmp_path, host):
    http_api.check_bind(config.Settings(db=tmp_path / "t.sqlite3", host=host))


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "example.com"])
def test_check_bind_refuses_public_without_tokens(tmp_path, host):
    with pytest.raises(ValueError, match="Từ chối"):
        http_api.check_bind(config.Settings(db=tmp_path / "t.sqlite3", host=host))


def test_check_bind_allows_public_with_a_token(tmp_path):
    http_api.check_bind(config.Settings(db=tmp_path / "t.sqlite3", host="0.0.0.0",
                                        api_token="x"))
    http_api.check_bind(config.Settings(db=tmp_path / "t.sqlite3", host="0.0.0.0",
                                        mcp_token="y"))


def test_ingest_countable_flag(client, api_settings, student):
    videos = copy.deepcopy(payload()["videos"])
    videos[1]["countable"] = False
    videos[0]["countable"] = True
    response = client.post("/ingest", json=payload(videos=videos), headers=auth(API_TOKEN))
    assert response.status_code == 200, response.text
    con = db.connect(api_settings.db)
    try:
        rows = {r["video_key"]: r["countable"] for r in con.execute("SELECT * FROM videos")}
    finally:
        con.close()
    assert rows == {YT_ID: 1, YT_ID2: 0}


@pytest.mark.parametrize("bad", ["false", 0, 1, None, []])
def test_ingest_countable_must_be_bool(client, api_settings, student, bad):
    videos = copy.deepcopy(payload()["videos"])
    videos[0]["countable"] = bad
    response = client.post("/ingest", json=payload(videos=videos), headers=auth(API_TOKEN))
    assert response.status_code == 400
    assert "countable" in response.json()["error"]
    assert counts(api_settings) == (0, 0)
