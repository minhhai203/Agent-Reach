"""MCP server: read-only tools via the in-memory SDK session, and the HTTP mount."""

from __future__ import annotations

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from starlette.testclient import TestClient

from conftest import vn
from video_tracker import config, core, db, http_api, mcp_server

API_TOKEN = "api-token-for-tests-123"
MCP_TOKEN = "mcp-token-for-tests-456"
NOW = vn(2026, 11, 18, 10, 0)
TOOLS = {"week_progress", "student_history", "list_students", "sync_status"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1"},
    },
}


@pytest.fixture
def mcp_settings(tmp_path) -> config.Settings:
    return config.Settings(
        db=tmp_path / "data" / "tracker.sqlite3",
        api_token=API_TOKEN,
        mcp_token=MCP_TOKEN,
        allowed_hosts=["testserver"],
    )


@pytest.fixture
def seeded(mcp_settings, freeze_time):
    freeze_time(NOW)
    con = db.connect(mcp_settings.db)
    try:
        lan = core.add_student(con, "Lan", 2, youtube_url="https://www.youtube.com/@lan")
        core.add_student(con, "Minh", 3)
        core.record_video(con, lan["id"], "https://youtu.be/dQw4w9WgXcQ", "2026-11-16")
        core.record_video(con, lan["id"], "https://youtu.be/abcdefghijk", "2026-11-17")
    finally:
        con.close()
    return mcp_settings


def run_session(settings, action):
    """Run ``await action(session)`` against a fresh in-memory client/server pair."""
    server = mcp_server.build_mcp(settings)
    result = {}

    async def main():
        async with create_connected_server_and_client_session(server) as session:
            result["value"] = await action(session)

    anyio.run(main)
    return result["value"]


def db_snapshot(settings):
    con = db.connect(settings.db)
    try:
        return tuple(
            con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("students", "videos", "sync_runs", "reports")
        )
    finally:
        con.close()


def test_exactly_four_read_only_tools(seeded):
    tools = run_session(seeded, lambda s: s.list_tools())
    assert {tool.name for tool in tools.tools} == TOOLS
    for tool in tools.tools:
        assert tool.annotations is not None and tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
    descriptions = {tool.name: tool.description for tool in tools.tools}
    for name in ("week_progress", "student_history", "sync_status"):
        assert "insufficient_data" in descriptions[name]
        assert "chưa đăng" in descriptions[name]


def test_week_progress_tool(seeded):
    before = db_snapshot(seeded)
    result = run_session(seeded, lambda s: s.call_tool("week_progress", {"week_offset": 0}))
    assert result.isError is False
    data = result.structuredContent
    assert data["week_start"] == "2026-11-16" and data["week_end"] == "2026-11-22"
    by_code = {s["code"]: s for s in data["students"]}
    assert by_code["S01"]["status"] == "insufficient_data" and by_code["S01"]["count"] == 2
    assert by_code["S02"]["status"] == "insufficient_data"
    assert db_snapshot(seeded) == before


def test_week_progress_rejects_future_week(seeded):
    result = run_session(seeded, lambda s: s.call_tool("week_progress", {"week_offset": 1}))
    assert result.isError is True
    assert "Không thể xem tiến độ của tuần trong tương lai." in result.content[0].text


def test_student_history_tool(seeded):
    result = run_session(
        seeded, lambda s: s.call_tool("student_history", {"student": "s01", "weeks": 2})
    )
    assert result.isError is False
    data = result.structuredContent
    assert data["student"]["code"] == "S01"
    assert [w["week_start"] for w in data["weeks"]] == ["2026-11-16", "2026-11-09"]

    by_id = run_session(seeded, lambda s: s.call_tool("student_history", {"student": "2"}))
    assert by_id.structuredContent["student"]["code"] == "S02"
    assert len(by_id.structuredContent["weeks"]) == 4


@pytest.mark.parametrize(
    "arguments,message",
    [
        ({"student": "S99"}, "Không tìm thấy học viên."),
        ({"student": "", "weeks": 2}, "Không tìm thấy học viên."),
        ({"student": "S01", "weeks": 0}, "Số tuần phải là số nguyên từ 1 đến 52."),
        ({"student": "S01", "weeks": 53}, "Số tuần phải là số nguyên từ 1 đến 52."),
    ],
)
def test_student_history_bad_input(seeded, arguments, message):
    result = run_session(seeded, lambda s: s.call_tool("student_history", arguments))
    assert result.isError is True
    assert message in result.content[0].text


def test_list_students_and_sync_status_tools(seeded):
    students = run_session(seeded, lambda s: s.call_tool("list_students", {}))
    assert [s["code"] for s in students.structuredContent["students"]] == ["S01", "S02"]
    status = run_session(seeded, lambda s: s.call_tool("sync_status", {}))
    rows = status.structuredContent["sync_status"]
    assert len(rows) == 1 and rows[0]["platform"] == "youtube" and rows[0]["synced"] is False


def test_no_write_tools_exist(seeded):
    result = run_session(seeded, lambda s: s.call_tool("add_student", {"name": "X"}))
    assert result.isError is True
    assert db_snapshot(seeded)[0] == 2


def test_weekly_report_prompt(seeded):
    prompts = run_session(seeded, lambda s: s.list_prompts())
    assert [p.name for p in prompts.prompts] == ["weekly_report"]
    prompt = run_session(seeded, lambda s: s.get_prompt("weekly_report"))
    text = prompt.messages[0].content.text
    assert "insufficient_data" in text and "chưa đăng" in text


def test_server_instructions_warn_about_insufficient_data(mcp_settings):
    mcp = mcp_server.build_mcp(mcp_settings)
    assert mcp.name == "video-tracker"
    assert "insufficient_data" in mcp.instructions and "chưa đăng" in mcp.instructions


def test_transport_security_hosts(tmp_path):
    settings = config.Settings(db=tmp_path / "t.sqlite3", port=9001,
                               allowed_hosts=["tracker.example.com", "vps:*"])
    security = mcp_server._transport_security(settings)
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == [
        "tracker.example.com", "vps:*", "127.0.0.1:9001", "localhost:9001", "[::1]:9001",
    ]
    assert "https://tracker.example.com" in security.allowed_origins
    assert "http://127.0.0.1:9001" in security.allowed_origins


# ---------------------------------------------------------------------------
# HTTP mount (/mcp)
# ---------------------------------------------------------------------------

@pytest.fixture
def http_client(seeded):
    with TestClient(http_api.build_app(seeded)) as client:  # runs the MCP lifespan
        yield client


def test_mcp_http_requires_token(http_client):
    response = http_client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"error": "Thiếu hoặc sai token xác thực."}


@pytest.mark.parametrize("token", [API_TOKEN, "", "wrong", MCP_TOKEN + "x"])
def test_mcp_http_rejects_other_tokens(http_client, token):
    response = http_client.post(
        "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert MCP_TOKEN not in response.text and API_TOKEN not in response.text


def test_mcp_http_initialize_and_call_with_mcp_token(http_client):
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {MCP_TOKEN}"}
    response = http_client.post("/mcp", json=INITIALIZE, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["serverInfo"]["name"] == "video-tracker"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert MCP_TOKEN not in response.text

    listed = http_client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers
    )
    assert {t["name"] for t in listed.json()["result"]["tools"]} == TOOLS

    called = http_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "week_progress", "arguments": {"week_offset": 0}}},
        headers=headers,
    )
    result = called.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["summary"]["students"] == 2


def test_mcp_http_rejects_unlisted_host_even_with_token(http_client):
    response = http_client.post(
        "/mcp", json=INITIALIZE,
        headers={**MCP_HEADERS, "Authorization": f"Bearer {MCP_TOKEN}", "Host": "evil.example"},
    )
    assert response.status_code == 421


def test_mcp_http_unauthenticated_never_reaches_session_manager(seeded, monkeypatch):
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    calls = []
    original = StreamableHTTPSessionManager.handle_request

    async def spy(self, scope, receive, send):
        calls.append(scope["path"])
        await original(self, scope, receive, send)

    monkeypatch.setattr(StreamableHTTPSessionManager, "handle_request", spy)
    with TestClient(http_api.build_app(seeded)) as client:
        assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 401
        assert client.get("/mcp").status_code == 401
        assert calls == []
        ok = client.post("/mcp", json=INITIALIZE,
                         headers={**MCP_HEADERS, "Authorization": f"Bearer {MCP_TOKEN}"})
        assert ok.status_code == 200
        assert calls == ["/mcp"]
