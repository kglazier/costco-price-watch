"""Inbound Telegram: pull receipt photos you send to the bot.

Outbound lives in notify.py. This half polls getUpdates, downloads any photos,
and hands back local file paths.

Telegram only retains updates for 24h and drops them once acknowledged, so the
offset cursor is persisted in the database — losing it means silently missing
receipts you already sent.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from . import config
from .notify import NotConfigured, _api


def _get(method: str, **params) -> dict:
    resp = httpx.get(_api(method), params=params, timeout=40)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body}")
    return body


def fetch_photos(offset: int | None = None) -> tuple[list[Path], int | None, int]:
    """Download every image sent to the bot since `offset`.

    Returns (paths, next_offset, skipped). Acknowledge by persisting
    next_offset — Telegram re-delivers until you do, and drops the update for
    good once you have.

    `skipped` counts messages carrying no usable image. It exists because
    advancing the cursor past a message we couldn't use is irreversible, so
    that has to be visible rather than silent.
    """
    config.ensure_dirs()
    params = {"timeout": 0, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset

    updates = _get("getUpdates", **params).get("result", [])
    if not updates:
        return [], offset, 0

    saved: list[Path] = []
    skipped = 0
    last_id = offset
    for update in updates:
        last_id = update["update_id"] + 1
        msg = update.get("message") or {}
        file_id = _image_file_id(msg)
        if not file_id:
            skipped += 1
            continue
        path = _download(file_id, msg.get("message_id", int(time.time())))
        if path:
            saved.append(path)
        else:
            skipped += 1

    return saved, last_id, skipped


def _image_file_id(msg: dict) -> str | None:
    """Pull an image out of a message, however it was sent.

    Telegram delivers a compressed image as `photo` but an uncompressed one as
    `document` — which is what you get sharing a screenshot straight from
    another app, or choosing "send as file". Handling only `photo` silently
    skipped those while still advancing the read cursor, so the image was
    consumed and lost rather than retried.
    """
    photos = msg.get("photo")
    if photos:
        # Several sizes are sent; the last is the largest.
        return photos[-1]["file_id"]

    doc = msg.get("document") or {}
    mime = (doc.get("mime_type") or "").lower()
    if doc.get("file_id") and mime.startswith("image/"):
        return doc["file_id"]

    return None


def _download(file_id: str, tag: int) -> Path | None:
    info = _get("getFile", file_id=file_id).get("result", {})
    remote = info.get("file_path")
    if not remote:
        return None

    url = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{remote}"
    resp = httpx.get(url, timeout=90)
    resp.raise_for_status()

    suffix = Path(remote).suffix or ".jpg"
    dest = config.INBOX / f"receipt-{tag}{suffix}"
    dest.write_bytes(resp.content)
    return dest


__all__ = ["fetch_photos", "NotConfigured"]
