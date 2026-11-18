"""Tests for video_tracker.deliver (no network, no real subprocess)."""

from __future__ import annotations

import json
import subprocess

import httpx
import pytest

from video_tracker import config, deliver as deliver_mod


def mk(**kw) -> config.Settings:
    return config.Settings(**kw)


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_stdout(capsys):
    res = deliver_mod.deliver("xin chào", mk(deliver="stdout"))
    assert res == {"ok": True, "channel": "stdout", "detail": res["detail"],
                   "parts_sent": 1, "parts_total": 1}
    assert "xin chào" in capsys.readouterr().out


def test_file_appends_with_separator(tmp_path, freeze_time):
    from conftest import vn

    freeze_time(vn(2026, 11, 18))
    target = tmp_path / "sub" / "dir" / "out.txt"
    s = mk(deliver="file", deliver_file=str(target))
    assert deliver_mod.deliver("một", s)["ok"]
    assert deliver_mod.deliver("hai", s)["ok"]
    content = target.read_text(encoding="utf-8")
    assert content.count("=====") == 4
    assert "2026-11-18T12:00:00+07:00" in content
    assert content.index("một") < content.index("hai")


def test_file_missing_path_is_config_error():
    with pytest.raises(ValueError, match="TRACKER_DELIVER_FILE"):
        deliver_mod.deliver("x", mk(deliver="file"))


def test_file_write_failure_returns_not_ok(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    res = deliver_mod.deliver("x", mk(deliver="file", deliver_file=str(blocker / "a.txt")))
    assert res["ok"] is False and res["channel"] == "file"


def test_webhook_success_and_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    s = mk(deliver="webhook", deliver_webhook_url="https://hook.test/x", deliver_webhook_token="tok-secret")
    res = deliver_mod.deliver("báo cáo", s, http_client=client_for(handler))
    assert res["ok"] and res["channel"] == "webhook"
    assert seen["body"] == {"text": "báo cáo"}
    assert seen["auth"] == "Bearer tok-secret"
    assert "tok-secret" not in json.dumps(res)


def test_webhook_without_token_has_no_auth_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(204)

    s = mk(deliver="webhook", deliver_webhook_url="https://hook.test/x")
    assert deliver_mod.deliver("x", s, http_client=client_for(handler))["ok"]
    assert seen["auth"] is None


def test_webhook_failures():
    s = mk(deliver="webhook", deliver_webhook_url="https://hook.test/x", deliver_webhook_token="tok-secret")
    res = deliver_mod.deliver("x", s, http_client=client_for(lambda r: httpx.Response(500)))
    assert res["ok"] is False and "500" in res["detail"]

    def boom(request):
        raise httpx.ConnectError("tok-secret refused", request=request)

    res = deliver_mod.deliver("x", s, http_client=client_for(boom))
    assert res["ok"] is False and "tok-secret" not in res["detail"]


def test_webhook_missing_url():
    with pytest.raises(ValueError, match="WEBHOOK_URL"):
        deliver_mod.deliver("x", mk(deliver="webhook"))


class FakeProc:
    def __init__(self, returncode=0, stderr=""):
        self.returncode, self.stderr, self.stdout = returncode, stderr, ""


def test_command_success(monkeypatch):
    calls = {}

    def fake_run(argv, **kw):
        calls["argv"], calls["kw"] = argv, kw
        return FakeProc(0)

    monkeypatch.setattr(deliver_mod.subprocess, "run", fake_run)
    s = mk(deliver="command", deliver_command="notify --to 'my team'")
    assert deliver_mod.deliver("nội dung", s)["ok"]
    assert calls["argv"] == ["notify", "--to", "my team"]
    assert calls["kw"]["input"] == "nội dung"
    assert calls["kw"]["timeout"] == 60
    assert not calls["kw"].get("shell")


def test_command_failures(monkeypatch):
    s = mk(deliver="command", deliver_command="notify", zalo_mod_token="")
    monkeypatch.setattr(deliver_mod.subprocess, "run", lambda a, **k: FakeProc(3, "lỗi gì đó"))
    res = deliver_mod.deliver("x", s)
    assert res["ok"] is False and "3" in res["detail"]

    def timeout(a, **k):
        raise subprocess.TimeoutExpired(a, 60)

    monkeypatch.setattr(deliver_mod.subprocess, "run", timeout)
    assert deliver_mod.deliver("x", s)["ok"] is False

    def missing(a, **k):
        raise FileNotFoundError("notify")

    monkeypatch.setattr(deliver_mod.subprocess, "run", missing)
    assert deliver_mod.deliver("x", s)["ok"] is False


def test_command_missing_config():
    with pytest.raises(ValueError, match="TRACKER_DELIVER_COMMAND"):
        deliver_mod.deliver("x", mk(deliver="command"))


def zalo(**kw) -> config.Settings:
    base = dict(
        deliver="zalo_mod",
        zalo_mod_url="http://127.0.0.1:18792/api/action",
        zalo_mod_token="zalo-secret",
        zalo_target_id="g123",
    )
    base.update(kw)
    return mk(**base)


def test_zalo_mod_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    res = deliver_mod.deliver("báo cáo", zalo(), http_client=client_for(handler))
    assert res["ok"] and res["channel"] == "zalo_mod"
    assert seen["url"] == "http://127.0.0.1:18792/api/action"
    assert seen["auth"] == "Bearer zalo-secret"
    assert seen["body"] == {
        "action": "send-message",
        "payload": {"targetId": "g123", "text": "báo cáo", "targetType": "group"},
    }


def test_zalo_mod_user_target_type():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    deliver_mod.deliver("x", zalo(zalo_target_type="user"), http_client=client_for(handler))
    assert seen["body"]["payload"]["targetType"] == "user"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"ok": False, "error": "zalo-secret g123 rớt"}),
        httpx.Response(200, json={"error": "bad"}),
        httpx.Response(200, json={"ok": False}),
        httpx.Response(500, text="oops"),
        httpx.Response(401, json={"ok": True}),
    ],
)
def test_zalo_mod_failure_cases(response):
    res = deliver_mod.deliver("x", zalo(), http_client=client_for(lambda r: response))
    assert res["ok"] is False and res["channel"] == "zalo_mod"
    assert "zalo-secret" not in res["detail"] and "g123" not in res["detail"]


def test_zalo_mod_connection_error():
    def boom(request):
        raise httpx.ConnectError("no", request=request)

    res = deliver_mod.deliver("x", zalo(), http_client=client_for(boom))
    assert res["ok"] is False


def test_zalo_mod_non_json_2xx_ok():
    res = deliver_mod.deliver("x", zalo(), http_client=client_for(lambda r: httpx.Response(200, text="ok")))
    assert res["ok"] is True


def test_zalo_mod_config_errors():
    with pytest.raises(ValueError, match="TOKEN"):
        deliver_mod.deliver("x", zalo(zalo_mod_token=""))
    with pytest.raises(ValueError, match="TARGET_ID"):
        deliver_mod.deliver("x", zalo(zalo_target_id=""))
    with pytest.raises(ValueError, match="TARGET_TYPE"):
        deliver_mod.deliver("x", zalo(zalo_target_type="channel"))


def test_unknown_channel():
    with pytest.raises(ValueError, match="không hợp lệ"):
        deliver_mod.deliver("x", mk(deliver="carrier-pigeon"))
