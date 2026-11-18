"""Deliver a report text through the configured channel."""

from __future__ import annotations

import os
import shlex
import subprocess

import httpx

from . import config

CHANNELS = ("stdout", "file", "webhook", "command", "zalo_mod")
_DETAIL_MAX = 200


def _secrets(settings: config.Settings) -> list[str]:
    """Every secret in Settings (api/mcp/ingest tokens, API keys, webhook/zalo values …)."""
    return config.secret_values(settings)


def _scrub(text: str, settings: config.Settings) -> str:
    for secret in _secrets(settings):
        text = text.replace(secret, "***")
    text = " ".join(str(text).split())
    return text[:_DETAIL_MAX]


def _result(ok: bool, channel: str, detail: str) -> dict:
    return {"ok": ok, "channel": channel, "detail": detail}


def _post(client: httpx.Client | None, url: str, headers: dict, body: dict) -> httpx.Response:
    if client is not None:
        return client.post(url, json=body, headers=headers, timeout=30)
    with httpx.Client(timeout=30) as own:
        return own.post(url, json=body, headers=headers)


def _deliver_stdout(text: str) -> dict:
    print(text)
    return _result(True, "stdout", "Đã in ra stdout.")


def _deliver_file(text: str, settings: config.Settings) -> dict:
    if not settings.deliver_file:
        raise ValueError("Thiếu TRACKER_DELIVER_FILE cho kênh file.")
    path = _expand(settings.deliver_file)
    try:
        # New directories 0700, a new file 0600 (reports contain student names).
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = config.now_vn().isoformat(timespec="seconds")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(f"\n===== {stamp} =====\n{text}\n")
    except OSError as exc:
        return _result(False, "file", f"Không ghi được file: {exc.__class__.__name__}")
    return _result(True, "file", f"Đã ghi nối vào {path}")


def _expand(value: str):
    from pathlib import Path

    return Path(value).expanduser()


def _deliver_webhook(text: str, settings: config.Settings, client: httpx.Client | None) -> dict:
    if not settings.deliver_webhook_url:
        raise ValueError("Thiếu TRACKER_DELIVER_WEBHOOK_URL cho kênh webhook.")
    headers = {"Content-Type": "application/json"}
    if settings.deliver_webhook_token:
        headers["Authorization"] = f"Bearer {settings.deliver_webhook_token}"
    try:
        response = _post(client, settings.deliver_webhook_url, headers, {"text": text})
    except httpx.HTTPError as exc:
        return _result(False, "webhook", f"Gọi webhook thất bại ({exc.__class__.__name__}).")
    if not 200 <= response.status_code < 300:
        return _result(False, "webhook", f"Webhook trả HTTP {response.status_code}.")
    return _result(True, "webhook", f"Webhook trả HTTP {response.status_code}.")


def _deliver_command(text: str, settings: config.Settings) -> dict:
    try:
        argv = shlex.split(settings.deliver_command)
    except ValueError:
        raise ValueError("TRACKER_DELIVER_COMMAND không hợp lệ (sai dấu nháy).") from None
    if not argv:
        raise ValueError("Thiếu TRACKER_DELIVER_COMMAND cho kênh command.")
    try:
        proc = subprocess.run(
            argv, input=text, text=True, capture_output=True, timeout=60, check=False,
            env=config.child_env(),
        )
    except subprocess.TimeoutExpired:
        return _result(False, "command", "Lệnh gửi quá thời gian (60 giây).")
    except OSError as exc:
        return _result(False, "command", f"Không chạy được lệnh: {exc.__class__.__name__}")
    if proc.returncode != 0:
        tail = _scrub(proc.stderr or "", settings)
        return _result(False, "command", f"Lệnh thoát mã {proc.returncode}. {tail}".strip())
    return _result(True, "command", "Lệnh gửi chạy xong (mã 0).")


def _deliver_zalo_mod(text: str, settings: config.Settings, client: httpx.Client | None) -> dict:
    if not settings.zalo_mod_token:
        raise ValueError("Thiếu TRACKER_ZALO_MOD_TOKEN cho kênh zalo_mod.")
    if not settings.zalo_target_id:
        raise ValueError("Thiếu TRACKER_ZALO_TARGET_ID cho kênh zalo_mod.")
    if settings.zalo_target_type not in ("group", "user"):
        raise ValueError("TRACKER_ZALO_TARGET_TYPE phải là group hoặc user.")
    headers = {
        "Authorization": f"Bearer {settings.zalo_mod_token}",
        "Content-Type": "application/json",
    }
    body = {
        "action": "send-message",
        "payload": {
            "targetId": settings.zalo_target_id,
            "text": text,
            "targetType": settings.zalo_target_type,
        },
    }
    try:
        response = _post(client, settings.zalo_mod_url, headers, body)
    except httpx.HTTPError as exc:
        return _result(False, "zalo_mod", f"Gọi zalo-mod thất bại ({exc.__class__.__name__}).")
    if not 200 <= response.status_code < 300:
        return _result(False, "zalo_mod", f"zalo-mod trả HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and (data.get("ok") is False or data.get("error")):
        reason = _scrub(str(data.get("error") or "ok=false"), settings)
        return _result(False, "zalo_mod", f"zalo-mod báo lỗi: {reason}")
    return _result(True, "zalo_mod", f"zalo-mod trả HTTP {response.status_code}.")


def _send_one(channel: str, text: str, settings: config.Settings,
              http_client: httpx.Client | None) -> dict:
    if channel == "webhook":
        return _deliver_webhook(text, settings, http_client)
    if channel == "command":
        return _deliver_command(text, settings)
    return _deliver_zalo_mod(text, settings, http_client)


def deliver(text: str, settings: config.Settings, http_client: httpx.Client | None = None,
            parts: list[str] | None = None) -> dict:
    """Send `text` via settings.deliver. Raises ValueError only on bad configuration.

    Chat-like channels (webhook, command, zalo_mod) send `parts` (default ``[text]``) as
    separate messages in order and stop at the first failure; stdout and file write the
    full `text` once. The result is ``{"ok","channel","detail","parts_sent","parts_total"}``
    and its detail never contains a configured secret.
    """
    channel = (settings.deliver or "stdout").strip().lower()
    if channel in ("stdout", "file"):
        result = _deliver_stdout(text) if channel == "stdout" else _deliver_file(text, settings)
        result["parts_sent"], result["parts_total"] = (1 if result["ok"] else 0), 1
    elif channel in ("webhook", "command", "zalo_mod"):
        messages = [p for p in (parts or []) if p] or [text]
        sent = 0
        result = _result(True, channel, "")
        for message in messages:
            result = _send_one(channel, message, settings, http_client)
            if not result["ok"]:
                break
            sent += 1
        if len(messages) > 1:
            if result["ok"]:
                result["detail"] = f"{result['detail']} Đã gửi {sent}/{len(messages)} phần."
            else:
                result["detail"] = (f"{result['detail']} Dừng ở phần {sent + 1}/{len(messages)}; "
                                    f"đã gửi {sent} phần.")
        result["parts_sent"], result["parts_total"] = sent, len(messages)
    else:
        raise ValueError(
            f"Kênh gửi '{settings.deliver}' không hợp lệ. Chọn một trong: {', '.join(CHANNELS)}."
        )
    result["detail"] = _scrub(result["detail"], settings)
    return result
