"""ChatGPT provider adapter.

This package is the *only* boundary between the content-intelligence pipeline
and the ChatGPT Web transport. Upper layers (``music_pipeline``) talk to a
``ChatProvider`` and never see sentinel/websocket/backend-endpoint/upload
details.

The accepted transport is the patched ``gpt2agent`` (see
``experiments/gpt2agent/GPT2AGENT_PARITY_REPORT.md``). It is consumed as a
dependency inside ``gpt2agent_provider.py``; the transport itself is not
copied here.
"""
from __future__ import annotations

from .protocol import (
    ChatJobResult,
    ChatProvider,
    JobConfig,
    ProviderError,
    TransportUnavailableError,
)
from .launch_gate import LaunchIntervalChatProvider

__all__ = [
    "ChatProvider",
    "ChatJobResult",
    "JobConfig",
    "ProviderError",
    "TransportUnavailableError",
    "LaunchIntervalChatProvider",
]
