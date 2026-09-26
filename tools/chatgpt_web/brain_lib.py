"""High-level brain primitives on top of gpt2agent transport.

Repo-local copy of the disposable probe harness. Uses the vendored
``gpt2agent`` transport. Only IDs/verdict metadata is persisted; no secrets,
no conversation transcripts.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "vendor"))

import probe_lib as pl

from gpt2agent.backend import BackendClient, _BASE  # noqa: E402
from gpt2agent.sse import ConversationClient  # noqa: E402

SANDBOX_LINK_RE = re.compile(r"sandbox:(/mnt/data/[^)\s>]+)")

PROJECT_IDS = {
    "test": "g-p-6a635a648b0c819191678a9c0cc8f3ab",
    "1231": "g-p-6a3d4bc51e9481918b7980670a2b6428",
    "compact": "g-p-6a3b4a6b20208191bc73c4919f1649e2",
}


def project_id(name: str) -> str:
    return PROJECT_IDS[name]


def make_client() -> BackendClient:
    return BackendClient()


async def upload_attachment(
    backend: BackendClient,
    path: Path,
    *,
    temporary: bool = False,
    project_id: str | None = None,
) -> dict:
    """Upload a file (ZIP unindexed if zip, else indexed) and return attachment desc."""
    conv = ConversationClient(backend)
    path = Path(path)
    size = path.stat().st_size
    if size == 0:
        raise ValueError("empty file")
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix.lower() == ".zip":
        return await asyncio.to_thread(
            _upload_unindexed_sync, backend, path, size, mime_type, temporary, project_id
        )
    return await conv.upload_attachment(path, temporary=temporary)


def _upload_unindexed_sync(
    backend: BackendClient,
    path: Path,
    size: int,
    mime_type: str,
    temporary: bool,
    project_id: str | None,
) -> dict:
    import json as _json

    session = backend._session
    backend._reload_token_if_stale()
    create = session.post(
        _BASE + "/backend-api/files",
        headers={"Content-Type": "application/json"},
        json={
            "file_name": path.name,
            "file_size": size,
            "use_case": "my_files",
            "timezone_offset_min": -480,
            "reset_rate_limits": False,
            "supports_direct_azure_multipart": False,
            "mime_type": mime_type,
            "entry_surface": "chat_composer",
            "selection_method": "file_picker",
            "client_resolved_mime_type": mime_type,
            "mime_resolution_source": "filename_extension",
            "store_in_library": False,
        },
        timeout=30,
    )
    if create.status_code != 200:
        raise RuntimeError(f"file create failed ({create.status_code})")
    created = create.json()
    file_id = created.get("file_id")
    upload_url = created.get("upload_url")
    if not file_id or not upload_url:
        raise RuntimeError("file create returned no file_id/upload_url")
    destination = upload_url if upload_url.startswith("http") else _BASE + upload_url
    route = destination.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    metadata = {
        "store_in_library": False,
        "is_temporary_chat": temporary,
        "is_project_thread": project_id is not None,
    }
    if project_id:
        metadata["gizmo_id"] = project_id
    processing = {
        "file_id": file_id,
        "use_case": "my_files",
        "index_for_retrieval": False,
        "file_name": path.name,
        "entry_surface": "chat_composer",
        "metadata": metadata,
    }
    with path.open("rb") as handle:
        files = {"file": (path.name, handle, mime_type)}
        if route == "upload_content_and_finalize":
            response = session.post(
                destination,
                files=files,
                data={
                    "upload_url": upload_url,
                    "file_id": file_id,
                    "file_name": path.name,
                    "use_case": "my_files",
                    "index_for_retrieval": "false",
                    "entry_surface": "chat_composer",
                    "metadata": _json.dumps(metadata),
                },
                timeout=300,
            )
        elif route == "upload_content_bytes":
            uploaded = session.post(
                destination, files=files, data={"upload_url": upload_url}, timeout=300
            )
            if uploaded.status_code not in (200, 201, 204):
                raise RuntimeError(f"byte upload failed ({uploaded.status_code})")
            response = session.post(
                _BASE + "/backend-api/files/process_upload_stream",
                headers={"Content-Type": "application/json"},
                json=processing,
                timeout=300,
            )
        else:
            from curl_cffi import requests as curl_requests

            direct_headers = {"Content-Type": mime_type}
            if "x-amz-algorithm" not in destination.lower():
                direct_headers.update({"x-ms-blob-type": "BlockBlob", "x-ms-version": "2020-04-08"})
            uploaded = curl_requests.put(
                destination, headers=direct_headers, data=path.read_bytes(),
                impersonate="chrome131", verify=True, timeout=300,
            )
            if uploaded.status_code not in (200, 201, 202, 204):
                raise RuntimeError(f"direct upload failed ({uploaded.status_code})")
            response = session.post(
                _BASE + "/backend-api/files/process_upload_stream",
                headers={"Content-Type": "application/json"},
                json=processing,
                timeout=300,
            )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"file processing failed ({response.status_code})")
    return {
        "id": file_id,
        "size": size,
        "name": path.name,
        "mime_type": mime_type,
        "file_token_size": None,
        "source": "local",
        "is_big_paste": False,
    }


async def download_sandbox_artifact(
    backend: BackendClient,
    conversation_id: str,
    response_text: str,
    message_id: str | None,
) -> dict | None:
    """Resolve sandbox: links in the response to a downloadable artifact."""
    links = SANDBOX_LINK_RE.findall(response_text or "") if response_text else []
    bare = re.findall(r"`?/mnt/data/[^)\s>`]+", response_text or "") if response_text else []
    bare = [b.strip("`") for b in bare if b.strip("`")]
    for p in bare:
        if p not in links:
            links.append(p)
    if not links and not message_id:
        return None
    # If no links in text, scan conversation detail for assistant/tool messages
    # that mention sandbox paths.
    if not links:
        try:
            detail = await asyncio.to_thread(
                backend.get, f"/backend-api/conversation/{conversation_id}"
            ) or {}
        except Exception:
            detail = {}
        links = []
        for node in (detail.get("mapping") or {}).values():
            msg = (node or {}).get("message") or {}
            for part in (msg.get("content") or {}).get("parts") or []:
                if isinstance(part, str):
                    links.extend(SANDBOX_LINK_RE.findall(part))
                    links.extend(re.findall(r"/mnt/data/[^)\s>`]+", part))
    seen = set()
    for sandbox_path in reversed(links):
        key = (message_id, sandbox_path)
        if key in seen:
            continue
        seen.add(key)
        try:
            query = urlencode({"message_id": message_id, "sandbox_path": sandbox_path})
            resolved = backend.get(
                f"/backend-api/conversation/{conversation_id}/interpreter/download?{query}"
            )
            download_url = (resolved or {}).get("download_url")
            if not download_url:
                continue
            resp = backend._session.get(download_url, timeout=120)
            resp.raise_for_status()
        except Exception:
            continue
        return {
            "bytes": resp.content,
            "name": Path(sandbox_path).name or "artifact.bin",
            "conversation_id": conversation_id,
            "message_id": message_id,
            "sandbox_path": sandbox_path,
        }
    return None


def save_state(tag: str, state: dict) -> Path:
    return pl.save_json(f"state_{tag}.json", state)


def load_state(tag: str) -> dict:
    p = PROBE_DIR / "state" / f"state_{tag}.json"
    if not p.exists():
        raise FileNotFoundError(f"no state for {tag}: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


PROBE_DIR = pl.PROBE_DIR


async def create_chat(
    tag: str,
    prompt: str,
    *,
    mode: str = "normal",
    project: str | None = None,
    attachments: list[dict] | None = None,
    temporary: bool = False,
) -> dict:
    backend = make_client()
    pid = project_id(project) if project else None
    extra = None
    gizmo_id = None
    if pid:
        gizmo_id = pid
        extra = {
            "gizmo_id": pid,
            "conversation_mode": {"kind": "gizmo_interaction", "gizmo_id": pid},
        }
    result = await pl.send_turn(
        backend,
        prompt,
        temporary=temporary,
        attachments=attachments,
        gizmo_id=gizmo_id,
        extra=extra,
        timeout=240,
    )
    state = {
        "tag": tag,
        "mode": mode,
        "project": project,
        "project_id": pid,
        "conversation_id": result.get("conv_id"),
        "message_id": result.get("message_id"),
        "sent_parent": result.get("sent_parent"),
        "text_head": result.get("text_head"),
        "code": result.get("code"),
        "error": result.get("error"),
    }
    save_state(tag, state)
    return state


async def continue_chat(
    tag: str,
    prompt: str,
    *,
    attachments: list[dict] | None = None,
    timeout: float = 240.0,
    state_override: dict | None = None,
) -> dict:
    state = state_override or load_state(tag)
    if not state.get("conversation_id"):
        raise RuntimeError(f"state for {tag} has no conversation_id")
    backend = make_client()
    pid = state.get("project_id")
    extra = None
    gizmo_id = None
    if pid:
        gizmo_id = pid
        extra = {
            "gizmo_id": pid,
            "conversation_mode": {"kind": "gizmo_interaction", "gizmo_id": pid},
        }
    result = await pl.send_turn(
        backend,
        prompt,
        temporary=False,
        conversation_id=state["conversation_id"],
        parent_message_id=state["message_id"],
        attachments=attachments,
        gizmo_id=gizmo_id,
        extra=extra,
        timeout=timeout,
    )
    state = dict(state)
    state["message_id"] = result.get("message_id") or state.get("message_id")
    state["last_text_head"] = result.get("text_head")
    state["code"] = result.get("code")
    state["error"] = result.get("error")
    save_state(tag, state)
    return result