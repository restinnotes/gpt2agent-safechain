"""Shared probe library for gpt2agent brain experiments.

Repo-local helper: talks to the ChatGPT Web backend directly through the
vendored ``gpt2agent`` transport (``vendor/gpt2agent``). No secrets are
logged. Metadata (ids only) is the durable state we keep.

This is a probe/tooling harness, not production integration.
No Playwright is used.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

_THIS = Path(__file__).resolve()
ROOT = _THIS.parents[2]
_VENDOR = ROOT / "vendor" / "gpt2agent"
if str(ROOT / "vendor") not in sys.path:
    sys.path.insert(0, str(ROOT / "vendor"))

from gpt2agent.backend import BackendClient, _BASE  # noqa: E402
from gpt2agent.sentinel import SentinelGate  # noqa: E402
from gpt2agent.sse import ConversationClient, _build_payload  # noqa: E402

MODEL = "gpt-5-6-thinking"
THINKING = "min"

PROBE_DIR = ROOT / "tools" / "chatgpt_web"


def ensure_dir(sub: str) -> Path:
    p = PROBE_DIR / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_json(name: str, data: Any, sub: str = "state") -> Path:
    p = ensure_dir(sub) / name
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


sentry_lock = asyncio.Lock()


async def sentinel_headers(backend: BackendClient) -> dict:
    async with sentry_lock:
        sentinel = await SentinelGate(backend).get_tokens()
    headers = dict(backend._session.headers)
    headers["Accept"] = "text/event-stream"
    headers["Content-Type"] = "application/json"
    headers["Openai-Sentinel-Chat-Requirements-Token"] = sentinel["chat-requirements"]
    if sentinel.get("proof"):
        headers["Openai-Sentinel-Proof-Token"] = sentinel["proof"]
    if sentinel.get("turnstile"):
        headers["Openai-Sentinel-Turnstile-Token"] = sentinel["turnstile"]
    return headers


def build_turn_payload(
    prompt: str,
    *,
    temporary: bool = False,
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
    message_id: str | None = None,
    attachments: list[dict] | None = None,
    project_id: str | None = None,
    project_origin: bool = False,
    gpt_origin: bool = False,
    gizmo_id: str | None = None,
    extra: dict | None = None,
) -> dict:
    payload = _build_payload(
        MODEL,
        [{"role": "user", "content": prompt}],
        temporary=temporary,
        thinking_effort=THINKING,
    )
    if message_id:
        payload["messages"][0]["id"] = message_id
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if parent_message_id:
        payload["parent_message_id"] = parent_message_id
    if attachments:
        payload["messages"][-1].setdefault("metadata", {})["attachments"] = attachments
        payload["attachment_mime_types"] = [
            a.get("mime_type") for a in attachments if a.get("mime_type")
        ]
    if project_id:
        payload["project_id"] = project_id
        payload["conversation_origin"] = {
            "type": "project_thread" if project_origin else "primary_assistant",
            "project_id": project_id,
        }
    if gizmo_id:
        payload["gizmo_id"] = gizmo_id
        if gpt_origin:
            payload["conversation_origin"] = {
                "type": "custom_gpt",
                "gizmo_id": gizmo_id,
            }
    if extra:
        payload.update(extra)
    return payload


async def send_turn(
    backend: BackendClient,
    prompt: str,
    *,
    temporary: bool = False,
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
    project_id: str | None = None,
    project_origin: bool = False,
    gpt_origin: bool = False,
    gizmo_id: str | None = None,
    attachments: list[dict] | None = None,
    timeout: float = 300.0,
    extra: dict | None = None,
) -> dict:
    """POST a turn and capture minimal metadata. Never logs full text bodies to disk.

    Returns dict with code, conv_id, message_id (assistant terminal id), parent_of_turn,
    text_head, error, raw_statuses.
    """
    from curl_cffi.requests import AsyncSession

    backend._reload_token_if_stale()
    headers = await sentinel_headers(backend)
    payload = build_turn_payload(
        prompt,
        temporary=temporary,
        conversation_id=conversation_id,
        parent_message_id=parent_message_id,
        attachments=attachments,
        project_id=project_id,
        project_origin=project_origin,
        gizmo_id=gizmo_id,
        gpt_origin=gpt_origin,
        extra=extra,
    )
    sent_parent = payload["parent_message_id"]
    sent_msg_id = payload["messages"][0]["id"]
    conv_id: str | None = None
    last_assistant_id: str | None = None
    last_assistant_text = ""
    done = False
    statuses: list[str] = []

    async with AsyncSession(impersonate="chrome131", verify=True) as s:
        async with s.stream(
            "POST",
            _BASE + "/backend-api/conversation",
            headers=headers,
            json=payload,
            timeout=timeout,
        ) as resp:
            if resp.status_code not in (200, 201):
                body = ""
                try:
                    body = (await resp.atext())[:400]
                except Exception:
                    pass
                return {
                    "code": resp.status_code,
                    "conv_id": None,
                    "message_id": None,
                    "sent_parent": sent_parent,
                    "sent_msg_id": sent_msg_id,
                    "text_head": "",
                    "error": body,
                    "statuses": [],
                }
            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if not conv_id:
                    conv_id = obj.get("conversation_id")
                if obj.get("type") == "stream_handoff":
                    continue
                v = obj.get("v")
                if isinstance(v, str):
                    last_assistant_text += v
                msg = None
                if isinstance(v, dict):
                    msg = v.get("message")
                elif obj.get("message") is not None:
                    msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                role = (msg.get("author") or {}).get("role")
                statuses.append(f"{role}:{msg.get('status')}")
                if role == "assistant" and msg.get("id"):
                    last_assistant_id = msg["id"]
                    parts = (msg.get("content") or {}).get("parts") or []
                    if parts and isinstance(parts[0], str):
                        last_assistant_text = parts[0]
    return {
        "code": 200 if done else 0,
        "done": done,
        "conv_id": conv_id,
        "message_id": last_assistant_id,
        "sent_parent": sent_parent,
        "sent_msg_id": sent_msg_id,
        "text_head": last_assistant_text[:500],
        "error": None,
        "statuses": statuses[-20:],
    }


async def get_conversation(backend: BackendClient, conv_id: str) -> dict:
    try:
        return await asyncio.to_thread(
            backend.get, f"/backend-api/conversation/{conv_id}"
        ) or {}
    except Exception as exc:
        return {"error": str(exc)}


async def list_conversations(backend: BackendClient, limit: int = 20, query: str = "") -> dict:
    q = f"?offset=0&limit={limit}&order=updated"
    if query:
        q += "&" + query
    try:
        data = await asyncio.to_thread(
            backend.get, "/backend-api/conversations" + q
        ) or {}
        items = [
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "update_time": c.get("update_time"),
                "is_archived": c.get("is_archived"),
                "project_id": c.get("project_id"),
            }
            for c in (data.get("items") or [])
        ]
        return {"ok": True, "items": items}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def list_projects(backend: BackendClient) -> dict:
    endpoints = [
        "/backend-api/projects",
        "/backend-api/me/projects",
        "/backend-api/projects/overview",
    ]
    out = {}
    for ep in endpoints:
        try:
            data = await asyncio.to_thread(backend.get, ep)
            out[ep] = data
        except Exception as exc:
            out[ep] = {"error": type(exc).__name__ + ": " + str(exc)[:200]}
    try:
        convs = await list_conversations(backend, limit=20)
        out["/backend-api/conversations?query"] = convs
    except Exception as exc:
        out["conversations"] = {"error": str(exc)}
    return out


def sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_zip(zip_path: Path, entries: dict[str, str]) -> None:
    import zipfile

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in entries.items():
            zf.writestr(name, text)