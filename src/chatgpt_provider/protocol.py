"""Provider protocol shared by the real gpt2agent adapter and the fake provider.

Upper layers only depend on this module. In particular they must NOT know:

- sentinel / POW protocol;
- Celsius websocket handoff;
- backend endpoints;
- upload URL mechanics;
- ChatGPT internal message format.

A job is one ChatGPT web turn: an optional set of uploaded attachments, an
optional expectation that the turn produces a downloadable artifact (ZIP),
and the final assistant text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Protocol, runtime_checkable


PreSubmitHook = Callable[[], Awaitable[None]]


class ProviderPhase(str, Enum):
    """Where in a ChatGPT turn a provider failure occurred.

    ``PRE_SUBMIT`` failures happen before any conversation POST — sentinel
    chat-requirements prepare/finalize, client initialization, attachment
    upload. The server has no record of the turn, so a fresh submit is safe.
    ``POST_SUBMIT`` failures happen after the job demonstrably finished.
    ``POST_SUBMIT_AMBIGUOUS`` means submit status is unknown (conversation
    POST/stream dropped, no explicit submit phase surfaced by the transport)
    — callers must not blindly re-submit. ``UNKNOWN`` carries no phase
    information and preserves legacy retryable/ambiguous semantics.
    """

    PRE_SUBMIT = "pre_submit"
    POST_SUBMIT = "post_submit"
    POST_SUBMIT_AMBIGUOUS = "post_submit_ambiguous"
    UNKNOWN = "unknown"


#: (retryable, ambiguous) defaults per phase, used only when the caller does
#: not pass those flags explicitly. UNKNOWN is conservative.
_PHASE_DEFAULTS = {
    ProviderPhase.PRE_SUBMIT: (True, False),
    ProviderPhase.POST_SUBMIT: (True, False),
    ProviderPhase.POST_SUBMIT_AMBIGUOUS: (False, True),
    ProviderPhase.UNKNOWN: (True, True),
}


def classify_conversation_phase(exc: BaseException) -> ProviderPhase:
    """Classify a failure that surfaced through the transport's submit call.

    The accepted gpt2agent transport runs sentinel/chat-requirements
    prepare+finalize and the /backend-api/conversation POST/stream inside one
    method and exposes no explicit submit phase, so we classify on the
    exception text. A failure that demonstrably stayed inside
    sentinel/chat-requirements (prepare or finalize) is PRE_SUBMIT — safe to
    retry. Everything else (conversation POST/stream, unknown) is
    POST_SUBMIT_AMBIGUOUS.
    """
    lowered = f"{type(exc).__name__}: {exc}".lower()
    if (
        "chat-requirements/prepare" in lowered
        or "chat-requirements/finalize" in lowered
    ):
        return ProviderPhase.PRE_SUBMIT
    return ProviderPhase.POST_SUBMIT_AMBIGUOUS


class ProviderError(RuntimeError):
    """A provider-level failure (network, auth, malformed result).

    ``phase`` says where the failure happened (see :class:`ProviderPhase`).
    ``retryable`` and ``ambiguous`` remain available for legacy callers and are
    derived from the phase when not passed explicitly.

    - ``retryable=True`` means the ChatGPT job demonstrably finished (or never
      started, i.e. PRE_SUBMIT) and a fresh submit is safe.
    - ``retryable=False`` means submit status is unknown (stream dropped after
      POST) — callers must not blindly re-submit.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool | None = None,
        ambiguous: bool | None = None,
        phase: ProviderPhase | str | None = None,
    ) -> None:
        super().__init__(message)
        if phase is not None:
            phase = phase if isinstance(phase, ProviderPhase) else ProviderPhase(phase)
            default_retryable, default_ambiguous = _PHASE_DEFAULTS[phase]
            self.phase = phase
            self.retryable = default_retryable if retryable is None else retryable
            self.ambiguous = default_ambiguous if ambiguous is None else ambiguous
        else:
            self.retryable = True if retryable is None else retryable
            self.ambiguous = (not self.retryable) if ambiguous is None else ambiguous
            self.phase = (
                ProviderPhase.POST_SUBMIT_AMBIGUOUS
                if not self.retryable and self.ambiguous
                else ProviderPhase.UNKNOWN
            )

    @classmethod
    def pre_submit(cls, message: str) -> "ProviderError":
        """PRE_SUBMIT failure: nothing was submitted, a fresh submit is safe."""
        return cls(message, phase=ProviderPhase.PRE_SUBMIT)

    @classmethod
    def post_submit_ambiguous(cls, message: str) -> "ProviderError":
        """Submit status unknown: callers must not blindly re-submit."""
        return cls(message, phase=ProviderPhase.POST_SUBMIT_AMBIGUOUS)


class TransportUnavailableError(ProviderError):
    """The gpt2agent transport could not be imported/initialised.

    Raised instead of a raw ``ImportError`` so upper layers can fail closed
    with a clear message and correct exit code.
    """


@dataclass(frozen=True)
class JobConfig:
    """Transport-level configuration for one ChatGPT turn.

    ``temporary=True`` means an isolated temporary chat (no history). Note the
    All intellectual jobs use ``temporary=True``. The accepted transport's
    Temporary Artifact Addendum covers attachment upload, code interpreter,
    generated artifact download, and history isolation in this mode.
    ``allow_persistent=True`` is an explicit compatibility escape hatch.
    """

    model: str
    thinking_effort: str | None = None
    temporary: bool = True
    allow_persistent: bool = False


@dataclass(frozen=True)
class ChatJobResult:
    """Outcome of one ChatGPT turn as seen by the pipeline.

    ``text`` is the final assistant markdown. ``artifact_bytes`` is the byte
    content of the single expected downloadable artifact (e.g. a content
    package ZIP). ``artifact_name`` is the filename reported by the sandbox.
    """

    text: str
    artifact_bytes: bytes | None = None
    artifact_name: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def has_artifact(self) -> bool:
        return self.artifact_bytes is not None


@runtime_checkable
class ChatProvider(Protocol):
    """Minimal surface the pipeline relies on.

    ``complete_chat`` runs one turn:

    - ``prompt`` is the frozen prompt text (verbatim, no transport wrapper).
    - ``config`` carries model / thinking effort / temporary flag.
    - ``attachments`` are local files uploaded before the turn.
     - ``expect_artifact`` asks the provider to download the sandbox artifact
       the turn produced and return it as ``ChatJobResult.artifact_bytes``. When
       false, a completed assistant turn returns immediately and never waits for or fails over a missing artifact.
     - ``pre_submit``, when supplied, must be awaited exactly once after client
       setup, attachment upload, and sentinel requirements finalize, immediately
       before the conversation POST that starts the intellectual turn.

    The provider is async so the real transport can share an event loop.
    """

    async def complete_chat(
        self,
        *,
        prompt: str,
        config: JobConfig,
        attachments: list[str] | None = None,
        expect_artifact: bool = False,
        pre_submit: PreSubmitHook | None = None,
    ) -> ChatJobResult:
        ...
