#!/usr/bin/env python
"""One-shot two-stage ChatGPT Web payload parity probe.

The request envelope mirrors the non-sensitive structure captured from the
current Chrome session. It deliberately omits the opaque browser_context
instance_id so this probe does not reuse or alter a browser/session identity.
Run once from the repository root with the repo's runtime environment.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "vendor"))

from curl_cffi.requests import AsyncSession  # noqa: E402

import gpt2agent.sse as sse  # noqa: E402
from gpt2agent.backend import BackendClient  # noqa: E402
from gpt2agent.runtime import AccountRuntime, account_key  # noqa: E402
from gpt2agent.sentinel import SentinelGate  # noqa: E402
from gpt2agent.sse import ConversationClient  # noqa: E402

MODEL = "gpt-5-6-thinking"
THINKING_EFFORT = "extended"
TIMEZONE = "America/New_York"
TIMEZONE_OFFSET_MIN = 240
LOCAL_FUNCTION_NAMES = ["local.continue_in_work"]
CLIENT_CONTEXTUAL_INFO = {
    "app_name": "chatgpt.com",
    "app_surface": "codex_browser",
    "has_web_push_capabilities": True,
    "web_push_notification_permission": "default",
}

PROMPT = """有一个水杯配对游戏。共有 4 种不同颜色的水杯，每种颜色各有两个。将同色的两个水杯分别放在上下两层，因此上下两层各有 4 个水杯。下层 4 个水杯按某个未知顺序排列，挑战者无法看到它们；上层水杯的颜色和位置则完全可见。游戏开始后，挑战者可以反复进行以下操作：

1. 向裁判询问当前有多少个位置满足“上下两个水杯颜色相同”。裁判只回答匹配位置的总数，不透露具体是哪些位置；
2. 根据目前获得的所有信息，挑战者可以选择交换上层任意两个相邻位置的水杯，注意只能是相邻，不能是任意两个。

当 4 个位置全部匹配时，游戏结束。问题：

挑战者应采用何种策略，才能保证对于下层水杯的任意排列都能完成配对？

所有能保证成功的策略中，最坏情况所需的交换次数最少是多少？

回答时请不要进行联网搜索，也不要写代码来辅助计算(包括思考过程中)。

假设答案是 x ，你需要给出严格的证明，为什么 x 可行，为什么小于 x 不可行。"""

EXPECTED_PREPARE_KEYS = {
    "client_prepare_state", "action", "is_do_not_remember", "model",
    "parent_message_id", "thinking_effort", "timezone", "timezone_offset_min",
    "local_function_names", "partial_query", "client_prepare_dispatch",
    "client_prepare_source",
}
EXPECTED_FINAL_KEYS = {
    "action", "is_do_not_remember", "model", "parent_message_id",
    "thinking_effort", "timezone", "timezone_offset_min",
    "client_contextual_info", "local_function_names", "messages",
    "supported_encodings", "client_prepare_state",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def mark_time() -> dict:
    return {"utc": utc_now(), "mono": time.monotonic()}


def chrome_message(message_id: str) -> dict:
    return {
        "author": {"metadata": {}, "name": None, "role": "user"},
        "channel": None,
        "content": {"content_type": "text", "parts": [PROMPT]},
        "create_time": time.time(),
        "end_turn": None,
        "id": message_id,
        "metadata": {},
        "recipient": "all",
        "status": "finished_successfully",
        "update_time": None,
        "weight": 1,
    }


def chrome_final_payload(model: str, messages: list[dict], **kwargs) -> dict:
    """Use the captured Chrome message and top-level envelope shape."""
    message_id = str(uuid4())
    message = chrome_message(message_id)
    if messages and isinstance(messages[-1].get("content"), str):
        message["content"]["parts"] = [messages[-1]["content"]]
    payload = {
        "action": "next",
        "is_do_not_remember": False,
        "model": model,
        "parent_message_id": kwargs.get("parent_message_id") or str(uuid4()),
        "thinking_effort": kwargs.get("thinking_effort") or THINKING_EFFORT,
        "timezone": TIMEZONE,
        "timezone_offset_min": TIMEZONE_OFFSET_MIN,
        "client_contextual_info": dict(CLIENT_CONTEXTUAL_INFO),
        "local_function_names": list(LOCAL_FUNCTION_NAMES),
        "messages": [message],
        "supported_encodings": ["v1"],
        "client_prepare_state": "sent",
    }
    return payload


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def classify_reasoning(frame: dict) -> set[str]:
    found: set[str] = set()
    known_types = {"reasoning", "reasoning_recap", "reasoning_summary", "thinking", "thinking_preamble"}
    known_channels = {"analysis", "reasoning", "thinking"}
    for node in walk(frame):
        if node.get("type") in known_types:
            found.add("event_type:" + str(node["type"]))
        if node.get("channel") in known_channels:
            found.add("channel:" + str(node["channel"]))
        if node.get("is_thinking_preamble_message") is True:
            found.add("is_thinking_preamble_message")
        metadata = node.get("metadata")
        if isinstance(metadata, dict):
            if metadata.get("is_thinking_preamble_message") is True:
                found.add("is_thinking_preamble_message")
            if metadata.get("channel") in known_channels:
                found.add("channel:" + str(metadata["channel"]))
        content = node.get("content")
        if isinstance(content, dict) and content.get("content_type") in {"reasoning", "analysis"}:
            found.add("content_type:" + str(content["content_type"]))
    return found


def has_user_facing_text(frame: dict) -> bool:
    for node in walk(frame):
        author, content = node.get("author"), node.get("content")
        if not isinstance(author, dict) or author.get("role") != "assistant" or not isinstance(content, dict):
            continue
        if content.get("content_type") not in {"text", "multimodal_text"}:
            continue
        metadata = node.get("metadata") or {}
        if isinstance(metadata, dict) and (
            metadata.get("is_thinking_preamble_message") is True
            or metadata.get("channel") in {"analysis", "reasoning", "thinking"}
        ):
            continue
        if any(isinstance(part, str) and part.strip() for part in content.get("parts") or []):
            return True
    return False


def last_final_answer(metadata: dict) -> str | None:
    messages = list(metadata.get("messages") or [])
    if isinstance(metadata.get("message"), dict):
        messages.append(metadata["message"])
    for message in reversed(messages):
        if not isinstance(message, dict) or (message.get("author") or {}).get("role") != "assistant":
            continue
        if message.get("status") != "finished_successfully" or message.get("end_turn") is not True:
            continue
        msg_meta = message.get("metadata") or {}
        if isinstance(msg_meta, dict) and (
            msg_meta.get("is_thinking_preamble_message") is True
            or msg_meta.get("channel") in {"analysis", "reasoning", "thinking"}
        ):
            continue
        content = message.get("content") or {}
        if content.get("content_type") not in {"text", "multimodal_text"}:
            continue
        answer = "\n".join(part for part in content.get("parts") or [] if isinstance(part, str))
        if answer.strip():
            return answer
    return None


def scrub_metadata(value):
    secret_names = {
        "authorization", "cookie", "cookie_header", "sentinel_token",
        "openai-sentinel-chat-requirements-token", "openai-sentinel-proof-token",
        "openai-sentinel-turnstile-token", "x-conduit-token", "conduit_token",
        "access_token", "bearer_token", "refresh_token", "cf_clearance",
        "id_token", "resume_conversation_token", "resume_token", "cloudflare_token",
        "cf_token", "cf_turnstile", "turnstile_token", "token",
    }
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).lower() in secret_names
                or "token" in str(key).lower()
                or str(key).lower().startswith("cf_")
                or "cloudflare" in str(key).lower()
                else scrub_metadata(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub_metadata(item) for item in value]
    return value


def main_result(obs: dict, turn_metadata: dict, answer: str | None, completion_returned: bool) -> dict:
    submit = obs.get("submit")
    finished = obs.get("finished")

    def elapsed(event):
        if not event or not submit:
            return None
        return round((event["mono"] - submit["mono"]) * 1000, 3)

    ste = turn_metadata.get("server_ste_metadata") or {}
    if not isinstance(ste, dict):
        ste = {}
    final_payload = obs.get("final_payload") or {}
    prepare_body = obs.get("prepare_body") or {}
    first_sse = obs.get("first_sse") or {}
    first_reasoning = obs.get("first_reasoning") or {}
    first_text = obs.get("first_text") or {}
    partial_query_parts = (
        (prepare_body.get("partial_query", {}).get("content") or {}).get("parts") or []
    )
    return {
        "prepare_http_status": obs.get("prepare_status"),
        "final_http_status": obs.get("final_status"),
        "prepare_post_count": obs.get("prepare_post_count"),
        "final_post_count": obs.get("final_post_count"),
        "prepare_body": {
            "top_level_keys": sorted(prepare_body),
            "client_prepare_state": prepare_body.get("client_prepare_state"),
            "action": prepare_body.get("action"),
            "is_do_not_remember": prepare_body.get("is_do_not_remember"),
            "model": prepare_body.get("model"),
            "thinking_effort": prepare_body.get("thinking_effort"),
            "timezone": prepare_body.get("timezone"),
            "timezone_offset_min": prepare_body.get("timezone_offset_min"),
            "parent_message_id_present": bool(prepare_body.get("parent_message_id")),
            "local_function_names": prepare_body.get("local_function_names"),
            "partial_query_shape": {
                "author_role": (prepare_body.get("partial_query", {}).get("author") or {}).get("role"),
                "content_type": (prepare_body.get("partial_query", {}).get("content") or {}).get("content_type"),
                "parts_count": len(partial_query_parts),
                "prompt_chars": len(partial_query_parts[0]) if partial_query_parts and isinstance(partial_query_parts[0], str) else 0,
                "id_present": bool(prepare_body.get("partial_query", {}).get("id")),
            },
            "client_prepare_dispatch": prepare_body.get("client_prepare_dispatch"),
            "client_prepare_source": prepare_body.get("client_prepare_source"),
        },
        "final_body": {
            "top_level_keys": sorted(final_payload),
            "action": final_payload.get("action"),
            "is_do_not_remember": final_payload.get("is_do_not_remember"),
            "model": final_payload.get("model"),
            "thinking_effort": final_payload.get("thinking_effort"),
            "timezone": final_payload.get("timezone"),
            "timezone_offset_min": final_payload.get("timezone_offset_min"),
            "parent_message_id_present": bool(final_payload.get("parent_message_id")),
            "client_contextual_info": final_payload.get("client_contextual_info"),
            "local_function_names": final_payload.get("local_function_names"),
            "messages_shape": [
                {
                    "top_level_keys": sorted(message),
                    "author_role": (message.get("author") or {}).get("role"),
                    "content_type": (message.get("content") or {}).get("content_type"),
                    "parts_count": len((message.get("content") or {}).get("parts") or []),
                    "prompt_chars": sum(len(part) for part in (message.get("content") or {}).get("parts") or [] if isinstance(part, str)),
                }
                for message in final_payload.get("messages") or []
            ],
            "supported_encodings": final_payload.get("supported_encodings"),
            "client_prepare_state": final_payload.get("client_prepare_state"),
            "browser_context": "omitted; opaque Chrome instance_id not copied",
            "tools_key_present": "tools" in final_payload,
            "attachments_present": "attachment_mime_types" in final_payload or any(
                (message.get("metadata") or {}).get("attachments") for message in final_payload.get("messages") or []
            ),
        },
        "turn_metadata": {key: ste.get(key) for key in ("tool_invoked", "tool_name", "turn_use_case", "turn_mode", "fast_convo")},
        "server_ste_metadata": scrub_metadata(ste),
        "reasoning_events": {
            "observed": bool(obs.get("reasoning_markers")),
            "markers": sorted(obs.get("reasoning_markers") or []),
        },
        "timing": {
            "submitted_at_utc": submit.get("utc") if submit else None,
            "first_sse_at_utc": first_sse.get("utc"),
            "first_sse_after_submit_ms": elapsed(obs.get("first_sse")),
            "first_reasoning_at_utc": first_reasoning.get("utc"),
            "first_reasoning_after_submit_ms": elapsed(obs.get("first_reasoning")),
            "first_text_at_utc": first_text.get("utc"),
            "first_text_after_submit_ms": elapsed(obs.get("first_text")),
            "finished_at_utc": finished.get("utc") if isinstance(finished, dict) else None,
            "total_after_submit_ms": elapsed(finished),
        },
        "answer": answer,
        "completion_returned": completion_returned,
        "error_type": obs.get("error_type"),
    }


async def run_once() -> None:
    logging.disable(logging.CRITICAL)
    obs = {
        "prepare_post_count": 0,
        "final_post_count": 0,
        "prepare_status": None,
        "final_status": None,
        "prepare_body": None,
        "final_payload": None,
        "submit": None,
        "first_sse": None,
        "first_reasoning": None,
        "first_text": None,
        "reasoning_markers": set(),
        "error_type": None,
        "finished": None,
    }
    original_build = sse._build_payload
    original_prepare = ConversationClient._prepare_conversation
    original_decoded_lines = sse.decoded_lines
    original_post = AsyncSession.post
    original_runtime_init = AccountRuntime.__init__
    sse._build_payload = chrome_final_payload

    def guarded_runtime_init(self, token, source):
        runtime_root = pathlib.Path(os.environ.get(
            "GPT2AGENT_RUNTIME_DIR", str(pathlib.Path.home() / ".gpt2agent" / "accounts")
        ))
        runtime_path = runtime_root / account_key(token, source) / "runtime.json"
        if not runtime_path.is_file():
            raise RuntimeError("existing account runtime missing; refusing identity initialization")
        original_runtime_init(self, token, source)

    AccountRuntime.__init__ = guarded_runtime_init

    async def observed_async_post(self, *args, **kwargs):
        url = args[0] if args else kwargs.get("url", "")
        path = str(url).split("?", 1)[0]
        if path.endswith("/backend-api/f/conversation/prepare"):
            if obs["prepare_post_count"] >= 1:
                raise RuntimeError("one-prepare guard refused a repeated prepare POST")
            obs["prepare_post_count"] += 1
        elif path.endswith("/backend-api/f/conversation"):
            if obs["final_post_count"] >= 1:
                raise RuntimeError("one-turn guard refused a second final POST")
            obs["final_post_count"] += 1
            body = kwargs.get("json") or {}
            if set(body) != EXPECTED_FINAL_KEYS:
                raise RuntimeError("final body shape assertion failed before submit")
            obs["final_payload"] = body
            obs["submit"] = mark_time()
        response = await original_post(self, *args, **kwargs)
        if path.endswith("/backend-api/f/conversation/prepare"):
            obs["prepare_status"] = response.status_code
        elif path.endswith("/backend-api/f/conversation"):
            obs["final_status"] = response.status_code
        return response

    AsyncSession.post = observed_async_post

    async def parity_prepare(self, session, payload: dict, headers: dict) -> None:
        runtime = self._backend._runtime
        runtime.check_backoff()
        payload["timezone"] = TIMEZONE
        payload["timezone_offset_min"] = TIMEZONE_OFFSET_MIN
        payload["supported_encodings"] = ["v1"]
        message = payload["messages"][0]
        prepare_body = {
            "client_prepare_state": "sent",
            "action": payload["action"],
            "is_do_not_remember": payload["is_do_not_remember"],
            "model": payload["model"],
            "parent_message_id": payload["parent_message_id"],
            "thinking_effort": payload["thinking_effort"],
            "timezone": payload["timezone"],
            "timezone_offset_min": payload["timezone_offset_min"],
            "local_function_names": list(payload["local_function_names"]),
            "partial_query": {
                "author": {"role": "user"},
                "content": message["content"],
                "id": message["id"],
            },
            "client_prepare_dispatch": "debounced",
            "client_prepare_source": "composer_editor_state",
        }
        if set(prepare_body) != EXPECTED_PREPARE_KEYS:
            raise RuntimeError("prepare body shape assertion failed before submit")
        obs["prepare_body"] = prepare_body
        prepare_headers = dict(headers, Accept="application/json")
        prepare_headers["x-conduit-token"] = "no-token"
        response = await session.post(
            sse._F_CONV_URL + "/prepare",
            headers=prepare_headers,
            json=prepare_body,
            timeout=30,
        )
        runtime.note_response(response)
        if response.status_code != 200:
            raise RuntimeError(f"prepare HTTP {response.status_code}")
        result = response.json()
        conduit_token = result.get("conduit_token") if isinstance(result, dict) else None
        if not isinstance(conduit_token, str) or not conduit_token:
            raise RuntimeError("prepare response omitted required transport state")
        headers["x-conduit-token"] = conduit_token
        payload["client_prepare_state"] = "success"
        if set(payload) != EXPECTED_FINAL_KEYS:
            raise RuntimeError("final body shape assertion failed after prepare")

    ConversationClient._prepare_conversation = parity_prepare

    async def observed_decoded_lines(response):
        async for line in original_decoded_lines(response):
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            if isinstance(line, str) and line.startswith("data:"):
                raw = line[5:].strip()
                if raw and raw != "[DONE]":
                    event_time = mark_time()
                    if obs["first_sse"] is None:
                        obs["first_sse"] = event_time
                    if not any(marker in raw.lower() for marker in (
                        "resume_conversation_token", "conduit_token", "authorization",
                        "cookie", "sentinel_token", "turnstile_token", "cf_clearance",
                        "cloudflare_token",
                    )):
                        try:
                            frame = json.loads(raw)
                        except Exception:
                            frame = None
                        if isinstance(frame, dict):
                            if frame.get("type") == "server_ste_metadata":
                                obs["server_ste_metadata"] = frame.get("metadata") or {}
                            markers = classify_reasoning(frame)
                            if markers:
                                obs["reasoning_markers"].update(markers)
                                if obs["first_reasoning"] is None:
                                    obs["first_reasoning"] = event_time
                            if obs["first_text"] is None and has_user_facing_text(frame):
                                obs["first_text"] = event_time
            yield line

    sse.decoded_lines = observed_decoded_lines
    backend = None
    client = None
    answer = None
    completion_returned = False
    try:
        backend = BackendClient()
        client = ConversationClient(backend)
        await client.complete(
            model=MODEL,
            messages=[{"role": "user", "content": PROMPT}],
            temporary=True,
            poll_async=False,
            thinking_effort=THINKING_EFFORT,
            attachments=None,
        )
        completion_returned = True
        answer = last_final_answer(client.last_turn_metadata)
        if client.last_turn_metadata.get("server_ste_metadata"):
            obs["server_ste_metadata"] = client.last_turn_metadata["server_ste_metadata"]
    except Exception as exc:
        obs["error_type"] = type(exc).__name__
    finally:
        obs["finished"] = mark_time()
        if backend is not None:
            try:
                await backend.aclose()
            except Exception:
                pass
        AsyncSession.post = original_post
        ConversationClient._prepare_conversation = original_prepare
        sse._build_payload = original_build
        sse.decoded_lines = original_decoded_lines
        AccountRuntime.__init__ = original_runtime_init

    turn_metadata = {"server_ste_metadata": obs.get("server_ste_metadata") or {}}
    try:
        result = main_result(obs, turn_metadata, answer, completion_returned)
    except Exception as exc:
        result = {
            "prepare_http_status": obs.get("prepare_status"),
            "final_http_status": obs.get("final_status"),
            "prepare_post_count": obs.get("prepare_post_count"),
            "final_post_count": obs.get("final_post_count"),
            "server_ste_metadata": scrub_metadata(obs.get("server_ste_metadata") or {}),
            "reasoning_events": {
                "observed": bool(obs.get("reasoning_markers")),
                "markers": sorted(obs.get("reasoning_markers") or []),
            },
            "answer": answer,
            "completion_returned": completion_returned,
            "error_type": obs.get("error_type"),
            "report_error_type": type(exc).__name__,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(run_once())
