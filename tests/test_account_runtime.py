import asyncio
import base64
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from curl_cffi.requests import Cookies

from gpt2agent.runtime import AccountRuntime, account_key
from gpt2agent.sse import ConversationClient, _build_payload, _IncompleteStreamError
from gpt2agent.frontend import FrontendDecoder
from chatgpt_provider.protocol import classify_conversation_phase, ProviderPhase


def token(subject, suffix="signature"):
    claim = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode()).decode().rstrip("=")
    return "header." + claim + "." + suffix


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("GPT2AGENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("GPT2AGENT_MIN_ACCOUNT_INTERVAL_SECONDS", "0")
    instance = AccountRuntime(token("account-a"), None)
    yield instance
    asyncio.run(instance.aclose())


def test_identity_survives_restart_refresh_and_isolates_accounts(runtime):
    refreshed = AccountRuntime(token("account-a", "refreshed"), None)
    other = AccountRuntime(token("account-b"), None)
    assert (runtime.device_id, runtime.session_id) == (refreshed.device_id, refreshed.session_id)
    assert runtime.key == account_key(token("account-a", "refreshed"), None)
    assert other.device_id != runtime.device_id
    assert other.directory != runtime.directory
    state = runtime.path.read_text()
    assert token("account-a") not in state
    asyncio.run(refreshed.aclose())
    asyncio.run(other.aclose())


def test_cookie_roundtrip_keeps_domain_path_and_excludes_other_hosts(runtime):
    session = SimpleNamespace(cookies=Cookies())
    session.cookies.set("same", "root", domain=".chatgpt.com", path="/")
    session.cookies.set("same", "backend", domain=".chatgpt.com", path="/backend-api")
    session.cookies.set("foreign", "secret", domain="example.com")
    runtime.save_cookies(session)
    restored = SimpleNamespace(cookies=Cookies())
    runtime.load_cookies(restored)
    assert {(c.name, c.value, c.path) for c in restored.cookies.jar} == {
        ("same", "root", "/"), ("same", "backend", "/backend-api")}


def test_retry_after_survives_restart(runtime):
    runtime.note_response(SimpleNamespace(status_code=429, headers={"Retry-After": "120"}))
    restarted = AccountRuntime(token("account-a"), None)
    with pytest.raises(RuntimeError, match="backoff active"):
        restarted.check_backoff()
    asyncio.run(restarted.aclose())


def test_shared_account_gate_serializes_clients(runtime):
    second = AccountRuntime(token("account-a"), None)
    active = peak = 0
    async def run(instance):
        nonlocal active, peak
        async with instance.account_scope(SimpleNamespace(cookies=Cookies())):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
    async def scenario():
        await asyncio.gather(run(runtime), run(second))
        await second.aclose()
    asyncio.run(scenario())
    assert peak == 1


def test_gate_holds_across_processes_and_releases_on_cancel(runtime):
    vendor = str(Path(__file__).resolve().parents[1] / "vendor")
    code = f"import sys; sys.path.insert(0, {vendor!r}); from gpt2agent.runtime import _try_lock; s=open(sys.argv[1], 'a+b'); print(_try_lock(s)); s.close()"
    async def scenario():
        session = SimpleNamespace(cookies=Cookies())
        async with runtime.account_scope(session):
            result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code,
                      str(runtime.directory / "account.lock")], capture_output=True, text=True)
            assert result.returncode == 0
            assert result.stdout.strip() == "False"
        async with runtime.account_scope(session):
            pass
    asyncio.run(scenario())


def test_pinned_timezone_offset_and_locale(monkeypatch, tmp_path):
    monkeypatch.setenv("GPT2AGENT_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("GPT2AGENT_TIMEZONE", "America/New_York")
    monkeypatch.setenv("GPT2AGENT_LOCALE", "zh-CN")
    instance = AccountRuntime(token("account-a"), None)
    assert instance.timezone_offset_min in (240, 300)
    monkeypatch.setenv("GPT2AGENT_TIMEZONE", "Asia/Tokyo")
    refreshed = AccountRuntime(token("account-a", "refresh"), None)
    assert refreshed.timezone == "America/New_York"
    assert refreshed.locale == "zh-CN"
    asyncio.run(instance.aclose())
    asyncio.run(refreshed.aclose())


def test_frontend_patches_keep_artifact_metadata_and_ignore_nontext_continuation():
    decoder = FrontendDecoder()
    decoder.decode({"p": "", "o": "add", "v": {"conversation_id": "temp", "message": {
        "id": "assistant", "author": {"role": "assistant"},
        "content": {"content_type": "text", "parts": ["hi"]}, "metadata": {}}}})
    decoder.decode({"p": "/message/content/parts/0", "o": "append", "v": " there"})
    assert decoder.decode({"v": "!"})[0]["message"]["content"]["parts"] == ["hi there!"]
    frames = decoder.decode({"o": "patch", "v": [
        {"p": "/message/metadata/artifact", "o": "add", "v": "sandbox:/mnt/data/a.zip"},
        {"p": "/message/status", "o": "add", "v": "finished_successfully"}]})
    assert frames[-1]["message"]["metadata"]["artifact"].endswith("a.zip")
    assert decoder.decode({"v": "extra"})[0]["message"]["status"] == "finished_successfullyextra"


@pytest.mark.parametrize("result", [None, {}, {"conduit_token": ""}])
def test_prepare_missing_token_never_calls_submit(runtime, result):
    events = []
    class Session:
        async def post(self, url, **kwargs):
            events.append(url)
            assert kwargs["json"]["parent_message_id"] == "existing-parent"
            assert kwargs["json"]["history_and_training_disabled"] is False
            return SimpleNamespace(status_code=200, headers={}, json=lambda: result)
    client = ConversationClient(SimpleNamespace(_runtime=runtime))
    payload = _build_payload("model", [{"role": "user", "content": "hi"}], temporary=False,
                             conversation_id="existing-conversation", parent_message_id="existing-parent")
    with pytest.raises(RuntimeError) as error:
        asyncio.run(client._prepare_conversation(Session(), payload, {}))
    assert classify_conversation_phase(error.value) is ProviderPhase.PRE_SUBMIT
    assert len(events) == 1 and events[0].endswith("/f/conversation/prepare")


def test_warm_async_session_reused_and_cookies_transferred(runtime, monkeypatch):
    from gpt2agent import runtime as module
    created = []
    class Session:
        def __init__(self, **kwargs):
            self.cookies = Cookies()
            created.append(kwargs)
        async def close(self):
            pass
        async def post(self, *args, **kwargs):
            self.cookies.set("warm", "yes", domain="chatgpt.com")
            return SimpleNamespace(status_code=200, headers={})
    monkeypatch.setattr(module, "AsyncSession", Session)
    async def scenario():
        backend = SimpleNamespace(cookies=Cookies())
        for _ in range(2):
            async with runtime.async_session(backend) as transport:
                await transport.post("https://chatgpt.com/test")
        assert backend.cookies.get("warm") == "yes"
        await runtime.aclose()
    asyncio.run(scenario())
    assert len(created) == 1


def test_frontend_stream_keeps_text_metadata_artifacts_and_submit_boundary(runtime, monkeypatch):
    from gpt2agent import sse
    events = []
    frames = [
        {"type": "server_ste_metadata", "metadata": {"model_slug": "actual-model"}},
        {"p": "", "o": "add", "v": {"conversation_id": "temporary", "message": {
            "id": "user", "author": {"role": "user"}, "content": {"content_type": "text", "parts": ["echo"]}}}},
        {"p": "", "o": "add", "v": {"conversation_id": "temporary", "message": {
            "id": "assistant", "author": {"role": "assistant"}, "status": "in_progress",
            "content": {"content_type": "text", "parts": ["hello"]}, "metadata": {}}}},
        {"p": "/message/content/parts/0", "o": "append", "v": " 中文"},
        {"o": "patch", "v": [
            {"p": "/message/metadata/artifact", "o": "add", "v": "sandbox:/mnt/data/test.zip"},
            {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
            {"p": "/message/end_turn", "o": "add", "v": True}]},
    ]
    class Response:
        status_code = 200
        headers = {}
        cookies = Cookies()
        def json(self):
            return {"conduit_token": "once"}
        async def aiter_lines(self):
            yield 'data: "v1"'
            for frame in frames:
                yield "data: " + json.dumps(frame)
            yield "data: [DONE]"
    class Session:
        cookies = Cookies()
        async def post(self, url, **kwargs):
            if url.endswith("/prepare"):
                events.append("prepare")
                assert kwargs["json"]["parent_message_id"] == "client-created-root"
            else:
                events.append("submit")
                assert url.endswith("/f/conversation")
                assert kwargs["headers"]["x-conduit-token"] == "once"
                assert kwargs["json"]["thinking_effort"] == "max"
                assert kwargs["json"]["messages"][-1]["metadata"]["attachments"][0]["id"] == "file"
            return Response()
    class Backend:
        _runtime = runtime
        _session = SimpleNamespace(headers={}, cookies=Cookies())
        def _reload_token_if_stale(self):
            pass
        @asynccontextmanager
        async def async_session(self):
            yield Session()
    class Gate:
        def __init__(self, backend):
            pass
        async def get_tokens(self):
            events.append("sentinel")
            return {"chat-requirements": "fresh"}
    monkeypatch.setattr(sse, "SentinelGate", Gate)
    async def before_submit():
        events.append("hook")
    client = ConversationClient(Backend())
    text = asyncio.run(client.complete("requested", [{"role": "user", "content": "echo"}],
                    thinking_effort="max", attachments=[{"id": "file"}], pre_submit=before_submit))
    assert text == "hello 中文"
    assert events == ["sentinel", "prepare", "hook", "submit"]
    assert client.last_turn_metadata["conversation_id"] == "temporary"
    assert client.last_turn_metadata["message_id"] == "assistant"
    assert client.last_turn_metadata["server_ste_metadata"]["model_slug"] == "actual-model"
    assert client.last_turn_metadata["message"]["metadata"]["artifact"].endswith("test.zip")


def test_runtime_requirements_identity_has_no_per_call_random_fields(runtime):
    first = runtime.requirements_config("consistent-ua")
    second = runtime.requirements_config("consistent-ua")
    for index in (0, 2, 4, 6, 7, 8, 10, 11, 12, 14, 16, 17):
        assert first[index] == second[index]
    assert first[14] == runtime.session_id


def test_blocked_scope_does_not_erase_persisted_cookies(runtime):
    session = SimpleNamespace(cookies=Cookies())
    session.cookies.set("warm", "yes", domain="chatgpt.com")
    runtime.save_cookies(session)
    runtime.backoff()
    async def scenario():
        with pytest.raises(RuntimeError, match="backoff active"):
            async with runtime.account_scope(SimpleNamespace(cookies=Cookies())):
                pytest.fail("must not acquire blocked scope")
    asyncio.run(scenario())
    runtime.load_cookies(session)
    assert session.cookies.get("warm") == "yes"


@pytest.mark.parametrize("final_status", [200, 403, 429])
def test_requirement_metadata_does_not_skip_finalize_but_refusal_stops(runtime, monkeypatch, final_status):
    from gpt2agent import sentinel
    events = []
    class Session:
        async def post(self, url, **kwargs):
            events.append(url.rsplit("/", 1)[-1])
            if url.endswith("/prepare"):
                return SimpleNamespace(status_code=200, headers={}, json=lambda: {
                    "prepare_token": "prepare", "turnstile": {"required": True},
                    "so": {"required": True}, "proofofwork": {"required": False}})
            return SimpleNamespace(status_code=final_status, headers={}, text="refused",
                                   json=lambda: {"token": "server-issued"})
    class Backend:
        _runtime = runtime
        _session = SimpleNamespace(headers={"User-Agent": "stable-ua"})
        @asynccontextmanager
        async def async_session(self):
            yield Session()
    async def warmed(session):
        pass
    monkeypatch.setattr(runtime, "warmup", warmed)
    monkeypatch.setattr(sentinel._pow, "get_requirements_token", lambda *args, **kwargs: "requirements")
    async def scenario():
        if final_status == 200:
            result = await sentinel.SentinelGate(Backend()).get_tokens()
            assert result["chat-requirements"] == "server-issued"
        else:
            with pytest.raises(RuntimeError, match=f"finalize HTTP {final_status}"):
                await sentinel.SentinelGate(Backend()).get_tokens()
            with pytest.raises(RuntimeError, match="backoff active"):
                runtime.check_backoff()
    asyncio.run(scenario())
    assert events == ["prepare", "finalize"]


def test_frontend_sparse_lists_and_metadata_append_match_upstream():
    decoder = FrontendDecoder()
    decoder.decode({"p": "", "o": "add", "v": {"message": {"metadata": {}, "content": {"parts": []}}}})
    decoder.decode({"p": "/message/content/parts/0", "o": "append", "v": "start"})
    decoder.decode({"v": " end"})
    decoder.decode({"p": "/message/metadata", "o": "append", "v": {"model_slug": "actual"}})
    result = decoder.decode({"p": "/message/metadata/content_references/-", "o": "add", "v": {"type": "file"}})[0]
    assert result["message"]["content"]["parts"] == ["start end"]
    assert result["message"]["metadata"]["model_slug"] == "actual"
    assert result["message"]["metadata"]["content_references"] == [{"type": "file"}]
