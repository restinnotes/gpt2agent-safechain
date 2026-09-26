from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from chatgpt_provider import gpt2agent_provider as provider_mod
from chatgpt_provider.gpt2agent_provider import GPT2AgentProvider
from chatgpt_provider.protocol import (
    JobConfig,
    ProviderError,
    ProviderPhase,
)


@pytest.fixture(autouse=True)
def _disable_elapsed_fallback_gate_for_transport_unit_tests(monkeypatch):
    """Individual tests opt in when exercising the real 60-second fallback gate."""
    monkeypatch.setenv("GPT2AGENT_SUSPECTED_FALLBACK_SECONDS", "0")


class _Response:
    content = b"real artifact"

    def raise_for_status(self):
        return None


class _Session:
    def get(self, url, timeout=120):
        return _Response()


class _Backend:
    def __init__(self):
        self._session = _Session()

    def get(self, path):
        if "interpreter/download" in path:
            return {"download_url": "https://download/artifact"}
        raise AssertionError(path)


class _Conversation:
    def __init__(self):
        self.last_turn_metadata = {
            "conversation_id": "temp-conversation",
            "message_id": "assistant-message",
            "temporary": True,
        }

    async def upload_attachment(self, path, temporary=True):
        return {"id": "file", "name": Path(path).name, "mime_type": "text/plain"}

    async def complete(self, *args, **kwargs):
        assert kwargs["temporary"] is True
        pre_submit = kwargs.get("pre_submit")
        if pre_submit is not None:
            await pre_submit()
        return "[download](sandbox:/mnt/data/result.zip)"


def test_temporary_live_turn_artifact_download_does_not_poll_history(tmp_work_dir):
    provider = GPT2AgentProvider()
    backend = _Backend()
    conversation = _Conversation()

    async def ensure_client():
        return backend, conversation

    provider._ensure_client = ensure_client
    provider._wait_for_submitted_artifact = lambda *args: pytest.fail(
        "temporary jobs must not depend on persisted history"
    )
    fixture = tmp_work_dir / "input.txt"
    fixture.write_text("nonce", encoding="utf-8")
    result = asyncio.run(provider.complete_chat(
        prompt="create artifact",
        config=JobConfig("gpt-5-6-thinking", "max", True),
        attachments=[str(fixture)],
        expect_artifact=True,
    ))
    assert result.artifact_bytes == b"real artifact"
    assert result.meta["conversation_id"] == "temp-conversation"


def test_pre_submit_hook_is_forwarded_after_upload_to_transport(tmp_work_dir):
    events = []
    provider = GPT2AgentProvider()
    backend = _Backend()

    class Conversation(_Conversation):
        async def upload_attachment(self, path, temporary=True):
            events.append("upload")
            return await super().upload_attachment(path, temporary=temporary)

        async def complete(self, *args, **kwargs):
            events.append("transport")
            return await super().complete(*args, **kwargs)

    async def ensure_client():
        events.append("client")
        return backend, Conversation()

    async def pre_submit():
        events.append("pre_submit")

    provider._ensure_client = ensure_client
    fixture = tmp_work_dir / "input.txt"
    fixture.write_text("nonce", encoding="utf-8")
    asyncio.run(provider.complete_chat(
        prompt="submit at the gate",
        config=JobConfig("gpt-5-6-thinking", "max", True),
        attachments=[str(fixture)],
        pre_submit=pre_submit,
    ))
    assert events == ["client", "upload", "transport", "pre_submit"]


def test_concurrent_identical_attachments_use_one_physical_upload(tmp_work_dir):
    provider = GPT2AgentProvider()
    fixture = tmp_work_dir / "shared-evidence.txt"
    fixture.write_text("same exact evidence", encoding="utf-8")
    upload_count = 0

    class Conversation:
        async def upload_attachment(self, path, temporary=True):
            nonlocal upload_count
            upload_count += 1
            await asyncio.sleep(0.01)
            return {
                "id": "shared-file-id",
                "name": Path(path).name,
                "mime_type": "text/plain",
            }

    async def gather_descriptors():
        return await asyncio.gather(*(
            provider._shared_attachment_descriptor(
                Conversation(), object(), fixture, temporary=True
            )
            for _ in range(10)
        ))

    descriptors = asyncio.run(gather_descriptors())
    assert upload_count == 1
    assert {item["id"] for item in descriptors} == {"shared-file-id"}
    assert len({id(item) for item in descriptors}) == 10


def test_indexing_error_falls_back_to_unindexed_exact_file_upload(
    tmp_work_dir, monkeypatch
):
    provider = GPT2AgentProvider()
    fixture = tmp_work_dir / "evidence.md"
    fixture.write_text("verbatim evidence", encoding="utf-8")
    fallback_calls = []

    class Conversation:
        async def upload_attachment(self, path, temporary=True):
            raise RuntimeError("file processing failed: file.indexing.error")

    def unindexed(backend, path, temporary):
        fallback_calls.append((backend, path, temporary))
        return {
            "id": "unindexed-file-id",
            "name": path.name,
            "mime_type": "text/markdown",
        }

    monkeypatch.setattr(provider, "_upload_unindexed_file_sync", unindexed)
    backend = object()
    descriptor = asyncio.run(provider._upload_attachment(
        Conversation(), backend, fixture, temporary=True
    ))
    assert descriptor["id"] == "unindexed-file-id"
    assert fallback_calls == [(backend, fixture, True)]


def test_non_indexing_attachment_error_does_not_use_unindexed_fallback(
    tmp_work_dir, monkeypatch
):
    provider = GPT2AgentProvider()
    fixture = tmp_work_dir / "evidence.md"
    fixture.write_text("verbatim evidence", encoding="utf-8")

    class Conversation:
        async def upload_attachment(self, path, temporary=True):
            raise RuntimeError("authentication failed")

    monkeypatch.setattr(
        provider,
        "_upload_unindexed_file_sync",
        lambda *_args: pytest.fail("unindexed fallback must be narrowly scoped"),
    )
    with pytest.raises(RuntimeError, match="authentication failed"):
        asyncio.run(provider._upload_attachment(
            Conversation(), object(), fixture, temporary=True
        ))


def test_transport_gate_runs_after_sentinel_and_immediately_before_post(monkeypatch):
    _backend_mod, sse_mod = provider_mod._load_gpt2agent(
        str(Path(__file__).resolve().parents[1] / "vendor")
    )
    events = []

    class Cookies:
        def update(self, _other):
            return None

    class Backend:
        def __init__(self):
            self._session = type(
                "Session", (), {"headers": {}, "cookies": Cookies()}
            )()
            self._runtime = type("Runtime", (), {
                "timezone": "UTC", "timezone_offset_min": 0,
                "check_backoff": lambda self: None,
                "note_response": lambda self, response: None,
            })()

        def async_session(self):
            return Session()

        def _reload_token_if_stale(self):
            return None

    class Gate:
        def __init__(self, _backend):
            pass

        async def get_tokens(self):
            events.append("sentinel")
            return {"chat-requirements": "token"}

    class Response:
        status_code = 200
        cookies = Cookies()

        def json(self):
            return {"conduit_token": "conduit"}

        async def aiter_lines(self):
            yield "data: [DONE]"

    class Session:
        def __init__(self, **_kwargs):
            self.cookies = Cookies()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            if url.endswith("/prepare"):
                events.append("prepare")
                return Response()
            assert url.endswith("/backend-api/f/conversation")
            assert _kwargs["headers"]["x-conduit-token"] == "conduit"
            events.append("post")
            return Response()

    async def pre_submit():
        events.append("gate")

    async def consume():
        client = sse_mod.ConversationClient(Backend())
        return [
            item
            async for item in client.stream(
                "model",
                [{"role": "user", "content": "prompt"}],
                pre_submit=pre_submit,
            )
        ]

    monkeypatch.setattr(sse_mod, "SentinelGate", Gate)
    assert asyncio.run(consume()) == []
    assert events == ["sentinel", "prepare", "gate", "post"]


def test_transport_does_not_detail_poll_empty_temporary_handoff(monkeypatch):
    _backend_mod, sse_mod = provider_mod._load_gpt2agent(
        str(Path(__file__).resolve().parents[1] / "vendor")
    )
    client = sse_mod.ConversationClient(object())

    async def stream(*_args, **_kwargs):
        yield {
            "_conversation_id": "temporary-conversation",
            "_stream_handoff": True,
            "_stream_handoff_topic": "topic",
        }

    async def empty_handoff(*_args, **_kwargs):
        return ""

    async def forbidden_history_poll(*_args, **_kwargs):
        pytest.fail("temporary handoff must not use conversation detail polling")

    monkeypatch.setattr(client, "stream", stream)
    monkeypatch.setattr(client, "_resume_handoff_topic", empty_handoff)
    monkeypatch.setattr(client, "_poll_async_response", forbidden_history_poll)

    result = asyncio.run(client.complete(
        "gpt-5-6-thinking",
        [{"role": "user", "content": "temporary"}],
        temporary=True,
    ))
    assert result == ""


def test_parallel_callers_queue_and_reuse_backend_with_distinct_turn_metadata(monkeypatch):
    active = 0
    max_active = 0
    backends = []
    conversations = []

    class Backend:
        def __init__(self):
            self._session = type("Session", (), {"close": lambda self: None})()
            backends.append(self)

    class Conversation:
        def __init__(self, backend):
            self.backend = backend
            self.last_turn_metadata = {
                "conversation_id": f"conversation-{len(conversations)}",
                "message_id": f"message-{len(conversations)}",
                "temporary": True,
            }
            conversations.append(self)

        async def complete(self, *_args, **kwargs):
            nonlocal active, max_active
            pre_submit = kwargs.get("pre_submit")
            if pre_submit is not None:
                await pre_submit()
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1
            return "complete"

    monkeypatch.setattr(
        provider_mod,
        "_load_gpt2agent",
        lambda _site_packages: (
            type("BackendModule", (), {"BackendClient": Backend}),
            type("SseModule", (), {"ConversationClient": Conversation}),
        ),
    )
    provider = GPT2AgentProvider()

    async def run_calls():
        return await asyncio.gather(*(
            provider.complete_chat(
                prompt=f"call-{index}",
                config=JobConfig("gpt-5-6-thinking", "max", True),
            )
            for index in range(2)
        ))

    results = asyncio.run(run_calls())

    assert max_active == 1
    assert len(backends) == 1
    assert len(conversations) == 2
    assert conversations[0].backend is conversations[1].backend
    assert len({result.meta["conversation_id"] for result in results}) == 2


def test_short_max_reasoning_turn_is_rejected_and_opens_process_latch():
    provider = GPT2AgentProvider()
    provider._suspected_fallback_seconds = 60.0
    ticks = iter((100.0, 105.0))
    provider._clock = lambda: next(ticks)
    backend = _Backend()
    conversation = _Conversation()
    ensure_count = 0

    async def ensure_client():
        nonlocal ensure_count
        ensure_count += 1
        return backend, conversation

    provider._ensure_client = ensure_client
    with pytest.raises(ProviderError, match="suspected ChatGPT fallback") as first:
        asyncio.run(provider.complete_chat(
            prompt="max reasoning result",
            config=JobConfig("gpt-5-6-thinking", "max", True),
        ))
    assert first.value.retryable is False
    assert "completed in 5.000s" in str(first.value)

    with pytest.raises(ProviderError, match="circuit is open") as second:
        asyncio.run(provider.complete_chat(
            prompt="must not submit",
            config=JobConfig("gpt-5-6-thinking", "max", True),
        ))
    assert second.value.phase is ProviderPhase.PRE_SUBMIT
    assert ensure_count == 1


def test_temporary_missing_artifact_is_ambiguous_not_retryable():
    provider = GPT2AgentProvider()
    backend = _Backend()
    conversation = _Conversation()

    async def ensure_client():
        return backend, conversation

    async def complete_without_link(*args, **kwargs):
        return "still finalizing"

    provider._ensure_client = ensure_client
    conversation.complete = complete_without_link
    with pytest.raises(ProviderError) as exc:
        asyncio.run(provider.complete_chat(
            prompt="create artifact",
            config=JobConfig("gpt-5-6-thinking", "max", True),
            expect_artifact=True,
        ))
    assert exc.value.retryable is False
    assert exc.value.ambiguous is True


def test_fast_temporary_missing_artifact_is_classified_as_fallback_first(monkeypatch):
    monkeypatch.setenv("GPT2AGENT_SUSPECTED_FALLBACK_SECONDS", "60")
    provider = GPT2AgentProvider()
    backend = _Backend()
    conversation = _Conversation()
    provider._clock = iter((100.0, 105.0)).__next__

    async def ensure_client():
        return backend, conversation

    async def complete_without_link(*args, **kwargs):
        callback = kwargs.get("pre_submit")
        if callback is not None:
            await callback()
        return "fallback without artifact"

    provider._ensure_client = ensure_client
    conversation.complete = complete_without_link
    with pytest.raises(ProviderError, match="suspected ChatGPT fallback") as exc:
        asyncio.run(provider.complete_chat(
            prompt="create artifact",
            config=JobConfig("gpt-5-6-thinking", "max", True),
            expect_artifact=True,
        ))
    assert exc.value.ambiguous is False
    assert exc.value.retryable is False


def test_temporary_json_text_is_captured_as_artifact():
    provider = GPT2AgentProvider()
    backend = _Backend()
    conversation = _Conversation()

    async def ensure_client():
        return backend, conversation

    async def complete_with_json(*args, **kwargs):
        return '{"status":"VERIFIED_COMPLETE"}'

    provider._ensure_client = ensure_client
    conversation.complete = complete_with_json
    result = asyncio.run(provider.complete_chat(
        prompt="create JSON artifact",
        config=JobConfig("gpt-5-6-thinking", "max", True),
        expect_artifact=True,
    ))
    assert result.artifact_bytes == b'{"status":"VERIFIED_COMPLETE"}'
    assert result.artifact_name == "turn_artifact.json"
    assert result.meta["artifact_from_json_text"] is True


def test_outer_turn_timeout_is_explicit_and_closes_backend(monkeypatch):
    monkeypatch.setenv("GPT2AGENT_TURN_TIMEOUT_SECONDS", "0.01")
    provider = GPT2AgentProvider()
    backend = _Backend()
    closed = []

    class Conversation(_Conversation):
        async def complete(self, *args, **kwargs):
            await kwargs["pre_submit"]()
            await asyncio.sleep(1)
            return "never reached"

    backend._session.close = lambda: closed.append(True)
    conversation = Conversation()

    async def ensure_client():
        provider._active_backend.set(backend)
        return backend, conversation

    provider._ensure_client = ensure_client
    with pytest.raises(ProviderError, match="outer deadline") as exc:
        asyncio.run(provider.complete_chat(
            prompt="slow turn",
            config=JobConfig("gpt-5-6-thinking", "max", True),
        ))
    assert exc.value.phase is ProviderPhase.POST_SUBMIT_AMBIGUOUS
    assert exc.value.retryable is False
    assert closed == [True]


# ---------------------------------------------------------------------------
# Phase classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase_marker",
    [
        "sentinel/chat-requirements/prepare HTTP 403",
        "sentinel/chat-requirements/finalize HTTP 403",
    ],
)
def test_conv_failure_in_sentinel_chat_requirements_is_pre_submit_safe_retry(
    phase_marker,
):
    provider = GPT2AgentProvider()
    backend = _Backend()
    conversation = _Conversation()

    async def ensure_client():
        return backend, conversation

    async def fail_in_sentinel(*args, **kwargs):
        raise RuntimeError(f"{phase_marker}: blocked")

    provider._ensure_client = ensure_client
    provider._recover_submitted_turn_artifact = lambda *a, **k: None
    conversation.complete = fail_in_sentinel
    with pytest.raises(ProviderError) as exc:
        asyncio.run(provider.complete_chat(
            prompt="hello",
            config=JobConfig("gpt-5-6-thinking", "max", True),
        ))
    assert exc.value.phase is ProviderPhase.PRE_SUBMIT
    assert exc.value.retryable is True
    assert exc.value.ambiguous is False


def test_conv_failure_post_submit_unknown_is_ambiguous():
    provider = GPT2AgentProvider()
    backend = _Backend()
    conversation = _Conversation()

    async def ensure_client():
        return backend, conversation

    async def fail_stream(*args, **kwargs):
        await kwargs["pre_submit"]()
        raise RuntimeError("stream dropped after POST: connection reset")

    provider._ensure_client = ensure_client
    provider._recover_submitted_turn_artifact = lambda *a, **k: pytest.fail(
        "temporary stream failures must never poll conversation history"
    )
    conversation.complete = fail_stream
    with pytest.raises(ProviderError) as exc:
        asyncio.run(provider.complete_chat(
            prompt="hello",
            config=JobConfig("gpt-5-6-thinking", "max", True),
        ))
    assert exc.value.phase is ProviderPhase.POST_SUBMIT_AMBIGUOUS
    assert exc.value.retryable is False
    assert exc.value.ambiguous is True


def test_first_429_opens_latch_before_queued_work_can_submit(monkeypatch):
    monkeypatch.setenv("GPT2AGENT_MAX_ACTIVE_TURNS", "1")
    provider = GPT2AgentProvider()
    ensure_count = 0
    submit_count = 0

    class Conversation(_Conversation):
        async def complete(self, *args, **kwargs):
            nonlocal submit_count
            await kwargs["pre_submit"]()
            submit_count += 1
            raise RuntimeError(
                "GET /backend-api/conversations returned 429 Too Many Requests"
            )

    async def ensure_client():
        nonlocal ensure_count
        ensure_count += 1
        return _Backend(), Conversation()

    provider._ensure_client = ensure_client

    async def run_calls():
        return await asyncio.gather(*(
            provider.complete_chat(
                prompt=f"call-{index}",
                config=JobConfig("gpt-5-6-thinking", "max", True),
            )
            for index in range(2)
        ), return_exceptions=True)

    results = asyncio.run(run_calls())
    assert all(isinstance(item, ProviderError) for item in results)
    assert submit_count == 1
    assert ensure_count == 1
    assert "429" in str(results[0])
    assert "circuit is open" in str(results[1])


# ---------------------------------------------------------------------------
# Readiness (no-intellectual-turn probe)
# ---------------------------------------------------------------------------


class _DummyConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSocketModule:
    def __init__(self, available):
        self.available = available

    def create_connection(self, address, timeout=None):
        if not self.available:
            raise OSError(f"connection refused: {address}")
        return _DummyConn()


class _ReadinessBackend:
    def __init__(
        self,
        source: Path | None = None,
        device_id: str = "22222222-3333-4444-5555-666666666666",
    ):
        self._token_source = source
        self._session = type(
            "_ReadinessSession",
            (),
            {"headers": {"OAI-Device-Id": device_id}},
        )()


class _FakeSentinelGate:
    def __init__(self):
        self.called = False

    async def get_tokens(self):
        self.called = True
        return {"chat-requirements": "token", "proof": "", "turnstile": ""}


def _clean_proxy_env(monkeypatch):
    for key in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(key, raising=False)


def test_readiness_detects_stale_localhost_proxy_and_selects_current(monkeypatch):
    _clean_proxy_env(monkeypatch)
    provider = GPT2AgentProvider()
    backend = _ReadinessBackend(device_id="11111111-2222-3333-4444-555555555555")

    async def ensure_client():
        return backend, _Conversation()

    provider._ensure_client = ensure_client

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr(provider_mod, "socket", _FakeSocketModule(available=False))
    report = asyncio.run(provider.check_readiness())
    assert report["ready"] is False
    assert [u["proxy"] for u in report["proxy_localhost_unreachable"]] == [
        "HTTP_PROXY"
    ]
    assert report["proxies"]["HTTP_PROXY"] == "http://127.0.0.1:9"

    monkeypatch.setenv("HTTP_PROXY", "http://user:secret@127.0.0.1:8080")
    monkeypatch.setattr(provider_mod, "socket", _FakeSocketModule(available=True))
    report2 = asyncio.run(provider.check_readiness())
    assert report2["ready"] is True
    assert report2["proxy_localhost_unreachable"] == []
    assert report2["proxies"]["HTTP_PROXY"] == "http://127.0.0.1:8080"
    assert "secret" not in report2["proxies"]["HTTP_PROXY"]
    assert report2["proxy_credentials_present"] is True


def test_readiness_never_submits_and_reports_stable_redacted_device_id(
    tmp_work_dir, monkeypatch
):
    _clean_proxy_env(monkeypatch)
    device_id = "11111111-2222-3333-4444-555555555555"
    auth = tmp_work_dir / "auth.json"
    auth.write_text('{"tokens": {"access_token": "dummy"}}', encoding="utf-8")

    provider = GPT2AgentProvider()
    backend = _ReadinessBackend(source=auth, device_id=device_id)
    conversation = _Conversation()

    async def ensure_client():
        return backend, conversation

    async def must_not_complete(*args, **kwargs):
        pytest.fail("ConversationClient.complete must never be invoked by readiness")

    conversation.complete = must_not_complete
    provider._ensure_client = ensure_client
    provider._sentinel_factory = lambda _b: pytest.fail(
        "sentinel probe must be opt-in"
    )

    report1 = asyncio.run(provider.check_readiness())
    report2 = asyncio.run(provider.check_readiness())
    assert report1["ready"] is True
    assert report1["credential_source_exists"] is True
    digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:16]
    assert report1["device_id_hash"] == report2["device_id_hash"] == f"sha256:{digest}"
    assert report1["device_id_known"] is True
    assert device_id not in json.dumps(report1)
    assert "sentinel" not in report1


def test_readiness_probes_sentinel_only_when_requested(monkeypatch):
    _clean_proxy_env(monkeypatch)
    provider = GPT2AgentProvider()
    backend = _ReadinessBackend()
    gate = _FakeSentinelGate()

    async def ensure_client():
        return backend, _Conversation()

    provider._ensure_client = ensure_client
    provider._sentinel_factory = lambda _b: gate

    report = asyncio.run(provider.check_readiness())
    assert gate.called is False
    assert "sentinel" not in report

    report2 = asyncio.run(provider.check_readiness(probe_sentinel=True))
    assert gate.called is True
    assert report2["sentinel"]["ok"] is True
    assert "chat-requirements" in report2["sentinel"]["keys"]
