import asyncio
import importlib.util
from pathlib import Path

import pytest

from chatgpt_provider.gpt2agent_provider import GPT2AgentProvider
from chatgpt_provider.protocol import ChatJobResult, ProviderError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("music_runner", ROOT / "tools/chatgpt_web/music_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_music_adapter_saves_utf8_and_artifact_and_refuses_duplicate_submit(tmp_path, monkeypatch):
    calls = []
    async def complete(self, **kwargs):
        calls.append(kwargs)
        await kwargs["pre_submit"]()
        return ChatJobResult("中文结果", b"artifact", "../result.zip", {"temporary": True})
    async def close(self):
        pass
    monkeypatch.setattr(GPT2AgentProvider, "complete_chat", complete)
    monkeypatch.setattr(GPT2AgentProvider, "aclose", close)
    jobs = [{"job_id": "test", "model": "explicit-model", "prompt": "中文输入", "expect_artifact": True}]
    result = asyncio.run(runner.run_jobs(tmp_path, jobs))
    assert result[0]["status"] == "ok"
    assert (tmp_path / "outputs/test.md").read_text(encoding="utf-8") == "中文结果"
    assert (tmp_path / "artifacts/test/result.zip").read_bytes() == b"artifact"
    with pytest.raises(RuntimeError, match="already reached"):
        asyncio.run(runner.run_jobs(tmp_path, jobs))
    assert len(calls) == 1


def test_ambiguous_failure_stops_batch_and_marker_prevents_replay(tmp_path, monkeypatch):
    calls = []
    async def complete(self, **kwargs):
        calls.append(kwargs)
        await kwargs["pre_submit"]()
        raise ProviderError.post_submit_ambiguous("connection lost")
    async def close(self):
        pass
    monkeypatch.setattr(GPT2AgentProvider, "complete_chat", complete)
    monkeypatch.setattr(GPT2AgentProvider, "aclose", close)
    jobs = [{"job_id": name, "model": "explicit-model", "prompt": name} for name in ("first", "queued")]
    result = asyncio.run(runner.run_jobs(tmp_path, jobs))
    assert len(result) == 1 and result[0]["ambiguous"] is True
    assert not (tmp_path / "meta/queued.submitted.json").exists()
    with pytest.raises(RuntimeError, match="already reached"):
        asyncio.run(runner.run_jobs(tmp_path, jobs))
    assert len(calls) == 1
