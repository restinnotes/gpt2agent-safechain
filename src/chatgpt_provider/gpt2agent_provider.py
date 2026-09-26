"""Thin provider adapter over the accepted gpt2agent transport.

This adapter only *calls into* the patched gpt2agent package (as a dependency)
and reproduces the one reverse-engineered read path that gpt2agent itself does
not expose (sandbox artifact download), exactly as validated in
``experiments/gpt2agent/run_live_tests.py`` / ``GPT2AGENT_PARITY_REPORT.md``
§6 (items 10-12). It does not re-implement sentinel / websocket / auth / upload
mechanics.

No Playwright is used anywhere.

Failures are classified by phase: sentinel/chat-requirements prepare/finalize,
client initialization and attachment upload are PRE_SUBMIT (safe retry); a
failure out of ``ConversationClient.complete`` stays POST_SUBMIT_AMBIGUOUS
unless the exception text proves it never left sentinel/chat-requirements.
``check_readiness()`` probes network/env/credential state without starting an
intellectual turn.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import mimetypes
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from .protocol import (
    ChatJobResult,
    JobConfig,
    ProviderError,
    ProviderPhase,
    PreSubmitHook,
    TransportUnavailableError,
    classify_conversation_phase,
)

_log = logging.getLogger(__name__)

_SANDBOX_LINK_RE = re.compile(r"sandbox:(/mnt/data/[^)\s>]+)")
_BASE = "https://chatgpt.com"
_ACCOUNT_PROTECTION_MARKERS = (
    "429",
    "too many requests",
    "rate limit",
    "temporarily limited access",
    "temporarily restricted",
    "为保障数据安全",
    "暂时限制",
)
_KNOWN_FALLBACK_MODEL_MARKERS = ("i-mini", "fallback")

#: Proxy env keys in precedence order. Both upper/lower variants are accepted;
#: the transport's curl_cffi subclients honour them via ``trust_env`` (fresh
#: process env), and we never inject a different proxy or silently fall back.
_PROXY_ENV_KEYS = (
    ("HTTP_PROXY", "http_proxy"),
    ("HTTPS_PROXY", "https_proxy"),
    ("ALL_PROXY", "all_proxy"),
)


def _effective_proxy_env() -> dict[str, str]:
    """Effective proxy endpoints from the fresh process env (uppercase labels)."""
    effective: dict[str, str] = {}
    for upper, lower in _PROXY_ENV_KEYS:
        value = os.environ.get(upper) or os.environ.get(lower)
        if value:
            effective[upper] = value.strip()
    return effective


def _redact_proxy_url(raw: str) -> str:
    """Strip userinfo (credentials) from a proxy endpoint, keeping the target."""
    raw = raw.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlparse(raw)
    except ValueError:
        return raw
    host = parts.hostname or ""
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    return urlunparse(
        (parts.scheme, netloc, parts.path or "", parts.params, parts.query, parts.fragment)
    )


def _proxy_has_credentials(raw: str) -> bool:
    raw = raw.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlparse(raw)
    except ValueError:
        return False
    return bool(parts.username)


def _proxy_host_port(raw: str) -> tuple[str, int] | None:
    raw = raw.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlparse(raw)
    except ValueError:
        return None
    host = parts.hostname
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return host, port


def _is_localhost_host(host: str) -> bool:
    host = (host or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")


def _tcp_connect_ok(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _looks_like_account_protection(exc: BaseException) -> bool:
    lowered = f"{type(exc).__name__}: {exc}".lower()
    return any(marker.lower() in lowered for marker in _ACCOUNT_PROTECTION_MARKERS)


def _resolved_model_slugs(payload: Any) -> list[str]:
    """Extract server-reported model slugs from captured live-turn metadata."""
    found: set[str] = set()
    model_keys = {"model_slug", "resolved_model_slug", "default_model_slug"}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in model_keys and isinstance(item, str) and item.strip():
                    found.add(item.strip())
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return sorted(found)


def _load_gpt2agent(site_packages: str | None):
    """Import the gpt2agent package, honouring an explicit venv site-packages.

    Returns (backend_module, sse_module). Raises TransportUnavailableError with
    an actionable message when the transport cannot be imported.
    """
    if "gpt2agent" not in sys.modules:
        # The repository keeps the patched transport under ``vendor`` while
        # binary/runtime dependencies (curl_cffi, websockets, ...) live in the
        # project venv.  Add the latter as a dependency path without changing
        # the caller's explicit transport selection.
        project_root = Path(__file__).resolve().parents[2]
        runtime_sp = project_root / ".venv" / "Lib" / "site-packages"
        if runtime_sp.is_dir() and str(runtime_sp) not in sys.path:
            sys.path.insert(0, str(runtime_sp))
        if site_packages and not sys.modules.get("gpt2agent"):
            sp = Path(site_packages)
            if not sp.is_dir():
                raise TransportUnavailableError(
                    f"gpt2agent site-packages dir not found: {sp}"
                )
            if str(sp) not in sys.path:
                sys.path.insert(0, str(sp))
    try:
        from gpt2agent import backend as backend_mod  # noqa: PLC0415
        from gpt2agent import sse as sse_mod  # noqa: PLC0415

        return backend_mod, sse_mod
    except Exception as exc:  # pragma: no cover - import plumbing
        raise TransportUnavailableError(
            "gpt2agent transport unavailable (is the accepted experiments venv "
            "configured via provider.gpt2agent.site_packages?). "
            f"Import failed: {exc}"
        ) from exc


class _ConcurrentCalls:
    """Per-turn client-scope guard under the provider account gate.

    The provider's outer account gate bounds intellectual-turn concurrency. This
    guard gives each entered scope a fresh BackendClient + ConversationClient
    and tears the backend down when the scope exits.
    """

    def __init__(self, provider: "GPT2AgentProvider") -> None:
        self._provider = provider

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        backend = self._provider._active_backend.get()
        if backend is not None:
            await asyncio.to_thread(self._provider._close_backend, backend)
            self._provider._active_backend.set(None)
        return False


class GPT2AgentProvider:
    """ChatProvider backed by the accepted gpt2agent transport.

    Constructing the provider does no network I/O; the BackendClient /
    ConversationClient are created lazily on first use.

    ``complete_chat`` is account-safe: calls have a small bounded concurrency
    even when upper layers queue broad sampling work. Every turn provisions a fresh
    BackendClient + ConversationClient and tears it down afterwards. A history
    429/account-protection response opens a process-lifetime circuit breaker so
    queued work cannot keep probing the protected account.

    ``check_readiness()`` is a no-intellectual-turn probe: it initialises the
    backend, reports the credential source (never the secret), a stable
    redacted device-id hash, and the effective proxy endpoints without
    credentials, failing fast when a configured localhost proxy is unreachable.
    It invokes ``SentinelGate.get_tokens`` only when requested and never calls
    ``ConversationClient.complete``.
    """

    def __init__(self, *, site_packages: str | None = None) -> None:
        self._site_packages = site_packages
        self._backend_mod: Any = None
        self._sse_mod: Any = None
        self._active_backend: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
            "gpt2agent_active_backend", default=None
        )
        try:
            self._max_active_turns = int(
                os.environ.get("GPT2AGENT_MAX_ACTIVE_TURNS", "10")
            )
        except ValueError as exc:
            raise ValueError("GPT2AGENT_MAX_ACTIVE_TURNS must be an integer") from exc
        if self._max_active_turns < 1:
            raise ValueError("GPT2AGENT_MAX_ACTIVE_TURNS must be >= 1")
        self._active_turn_gate = asyncio.Semaphore(self._max_active_turns)
        self._lock = _ConcurrentCalls(self)
        self._sentinel_factory: Any = None
        self._account_protection_latch: str | None = None
        self._clock = time.perf_counter
        try:
            self._suspected_fallback_seconds = max(
                0.0,
                float(os.environ.get("GPT2AGENT_SUSPECTED_FALLBACK_SECONDS", "0")),
            )
        except ValueError as exc:
            raise ValueError(
                "GPT2AGENT_SUSPECTED_FALLBACK_SECONDS must be numeric"
            ) from exc
        # Canonical convergence launches multiple independent conversations
        # with the same evidence bundle.  A descriptor is account-scoped, so
        # uploading/indexing identical bytes once is both sufficient and much
        # gentler on ChatGPT's file service than an upload per conversation.
        # This cache deliberately lives only for the provider process.
        self._attachment_cache: dict[tuple[str, int, str, bool], dict] = {}
        self._attachment_uploads: dict[
            tuple[str, int, str, bool], asyncio.Task[dict]
        ] = {}

    async def _ensure_client(self) -> tuple[Any, Any]:
        backend_mod, sse_mod = _load_gpt2agent(self._site_packages)
        self._backend_mod = backend_mod
        self._sse_mod = sse_mod
        backend = await asyncio.to_thread(backend_mod.BackendClient)
        conv = sse_mod.ConversationClient(backend)
        self._active_backend.set(backend)
        return backend, conv

    @staticmethod
    def _close_backend(backend: Any) -> None:
        close = getattr(getattr(backend, "_session", None), "close", None)
        if close is not None:
            close()

    async def check_readiness(self, *, probe_sentinel: bool = False) -> dict:
        """Probe transport readiness WITHOUT starting an intellectual turn.

        Initialises the backend, then reports:

        - ``credential_source`` / ``credential_source_exists`` — where the
          bearer token comes from (the file path, never the secret itself);
        - ``device_id_known`` / ``device_id_hash`` — a stable redacted sha256
          of the ``OAI-Device-Id`` (the raw id is never echoed);
        - ``proxies`` — effective HTTP_PROXY/HTTPS_PROXY/ALL_PROXY endpoints
          with credentials stripped, read fresh from the process env so all
          network subclients share the same env;
        - ``proxy_localhost_unreachable`` / ``ready`` — a configured localhost
          proxy that cannot be reached is detected fail-fast and reported
          instead of silently falling back to a direct connection.

        ``SentinelGate.get_tokens`` is only invoked when ``probe_sentinel=True``.
        ``ConversationClient.complete`` is never called here.
        """
        async with self._lock:
            try:
                backend, _conv = await self._ensure_client()
            except Exception as exc:
                raise ProviderError(
                    f"gpt2agent client initialization failed: {exc}",
                    phase=ProviderPhase.PRE_SUBMIT,
                ) from exc

            report: dict[str, Any] = {"ready": True, "errors": []}

            source = getattr(backend, "_token_source", None)
            if source is not None:
                report["credential_source_exists"] = Path(source).is_file()
                report["credential_source"] = str(source)
            else:
                report["credential_source_exists"] = False
                report["credential_source"] = None

            session = getattr(backend, "_session", None)
            headers = getattr(session, "headers", None) or {}
            device_id = headers.get("OAI-Device-Id")
            report["device_id_known"] = bool(device_id)
            if device_id:
                digest = hashlib.sha256(str(device_id).encode("utf-8")).hexdigest()
                report["device_id_hash"] = f"sha256:{digest[:16]}"
            else:
                report["device_id_hash"] = None

            proxies = _effective_proxy_env()
            report["proxies"] = {k: _redact_proxy_url(v) for k, v in proxies.items()}
            report["proxy_credentials_present"] = any(
                _proxy_has_credentials(v) for v in proxies.values()
            )

            unreachable: list[dict] = []
            for key, raw in proxies.items():
                host_port = _proxy_host_port(raw)
                if host_port is None or not _is_localhost_host(host_port[0]):
                    continue
                host, port = host_port
                timeout = float(
                    os.environ.get("GPT2AGENT_PROXY_CHECK_TIMEOUT_SECONDS", "1")
                )
                if not _tcp_connect_ok(host, port, max(timeout, 0)):
                    unreachable.append({"proxy": key, "host": host, "port": port})
            report["proxy_localhost_unreachable"] = unreachable
            if unreachable:
                report["ready"] = False
                report["errors"].append(
                    "configured localhost proxy unreachable; not falling back: "
                    + "; ".join(f"{u['proxy']}={u['host']}:{u['port']}" for u in unreachable)
                )

            if probe_sentinel:
                gate = self._make_sentinel_gate(backend)
                try:
                    tokens = await gate.get_tokens()
                    report["sentinel"] = {
                        "ok": True,
                        "keys": sorted(str(k) for k in tokens),
                    }
                except Exception as exc:
                    report["sentinel"] = {
                        "ok": False,
                        "error": str(exc),
                        "phase": classify_conversation_phase(exc).value,
                    }
                    report["ready"] = False
                    report["errors"].append(f"sentinel probe failed: {exc}")

            return report

    def _make_sentinel_gate(self, backend: Any) -> Any:
        """Construct a ``SentinelGate`` bound to ``backend`` (lazy transport import)."""
        if self._sentinel_factory is not None:
            return self._sentinel_factory(backend)
        sse_mod = self._sse_mod
        cls = getattr(sse_mod, "SentinelGate", None)
        if cls is None:
            _, sse_mod = _load_gpt2agent(self._site_packages)
            cls = getattr(sse_mod, "SentinelGate", None)
        if cls is None:
            raise ProviderError(
                "gpt2agent sentinel gate unavailable",
                phase=ProviderPhase.PRE_SUBMIT,
            )
        return cls(backend)

    async def complete_chat(
        self,
        *,
        prompt: str,
        config: JobConfig,
        attachments: list[str] | None = None,
        expect_artifact: bool = False,
        pre_submit: PreSubmitHook | None = None,
    ) -> ChatJobResult:
        """Run one account-safe turn and fail closed on protection/fallback signals."""
        if attachments and os.environ.get("GPT2AGENT_TEXT_ONLY", "").lower() in {"1", "true", "yes"}:
            inline_parts: list[str] = []
            for raw_path in attachments:
                path = Path(raw_path)
                try:
                    text = path.read_bytes().decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    size = path.stat().st_size if path.exists() else 0
                    text = f"[binary attachment omitted: {path.name}; bytes={size}]"
                inline_parts.append(f"\n\n--- INLINE INPUT: {path.name} ---\n{text}\n--- END INLINE INPUT ---")
            prompt = prompt + "".join(inline_parts)
            attachments = None
        async with self._active_turn_gate:
            return await self._complete_chat_account_safe(
                prompt=prompt,
                config=config,
                attachments=attachments,
                expect_artifact=expect_artifact,
                pre_submit=pre_submit,
            )

    async def _complete_chat_account_safe(
        self,
        *,
        prompt: str,
        config: JobConfig,
        attachments: list[str] | None = None,
        expect_artifact: bool = False,
        pre_submit: PreSubmitHook | None = None,
    ) -> ChatJobResult:
        try:
            result = await self._complete_chat_once(
                prompt=prompt,
                config=config,
                attachments=attachments,
                expect_artifact=expect_artifact,
                pre_submit=pre_submit,
            )
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError) and _looks_like_account_protection(
                exc
            ):
                self._account_protection_latch = (
                    "ChatGPT conversation/history account protection was observed; "
                    "restart only after a genuine quiet period"
                )
            raise

        elapsed = (result.meta or {}).get("turn_elapsed_seconds")
        slugs = list((result.meta or {}).get("resolved_model_slugs") or [])
        fallback_reasons: list[str] = []
        if (
            self._suspected_fallback_seconds > 0
            and isinstance(elapsed, (int, float))
            and elapsed < self._suspected_fallback_seconds
        ):
            fallback_reasons.append(
                f"turn completed in {elapsed:.3f}s, below the "
                f"{self._suspected_fallback_seconds:.3f}s floor"
            )
        fallback_slugs = [
            slug
            for slug in slugs
            if any(marker in slug.lower() for marker in _KNOWN_FALLBACK_MODEL_MARKERS)
        ]
        if fallback_slugs:
            fallback_reasons.append(
                "server-reported fallback model slug(s): " + ", ".join(fallback_slugs)
            )
        if fallback_reasons:
            self._account_protection_latch = "suspected model fallback after submit"
            signature = hashlib.sha256((result.text or "").encode("utf-8")).hexdigest()
            raise ProviderError(
                "suspected ChatGPT fallback; result rejected and further account calls "
                "latched for this process: "
                + "; ".join(fallback_reasons)
                + f"; result_text_sha256={signature}",
                retryable=False,
                ambiguous=False,
                phase=ProviderPhase.POST_SUBMIT,
            )
        return result

    async def _complete_chat_once(
        self,
        *,
        prompt: str,
        config: JobConfig,
        attachments: list[str] | None = None,
        expect_artifact: bool = False,
        pre_submit: PreSubmitHook | None = None,
    ) -> ChatJobResult:
        if not config.temporary and not config.allow_persistent:
            raise ProviderError(
                "GPT2AgentProvider requires temporary=True",
                retryable=False,
                ambiguous=False,
            )
        async with self._lock:
            if self._account_protection_latch is not None:
                raise ProviderError(
                    "gpt2agent account-protection circuit is open; no request was "
                    f"submitted ({self._account_protection_latch})",
                    retryable=False,
                    ambiguous=False,
                    phase=ProviderPhase.PRE_SUBMIT,
                )
            try:
                backend, conv = await self._ensure_client()
            except Exception as exc:
                raise ProviderError(
                    f"gpt2agent client initialization failed: {exc}",
                    phase=ProviderPhase.PRE_SUBMIT,
                ) from exc

            known_conversations: set[str] = set()
            if expect_artifact and not config.temporary:
                known_conversations = await asyncio.to_thread(
                    self._recent_conversation_ids, backend
                )
            attachment_descs: list[dict] | None = None
            if attachments:
                try:
                    attachment_descs = [
                        await self._shared_attachment_descriptor(
                            conv, backend, Path(a), temporary=config.temporary
                        )
                        for a in attachments
                    ]
                except Exception as exc:
                    raise ProviderError(
                        f"gpt2agent attachment upload failed: {exc}",
                        phase=ProviderPhase.PRE_SUBMIT,
                    ) from exc
            pre_submit_completed = False
            submitted_at: float | None = None

            async def at_conversation_submit() -> None:
                nonlocal pre_submit_completed, submitted_at
                if pre_submit is not None:
                    await pre_submit()
                pre_submit_completed = True
                submitted_at = self._clock()

            try:
                # The websocket client historically had no outer deadline;
                # a half-closed connection could therefore leave a pipeline
                # worker alive forever. Keep the existing stream/recovery
                # semantics, but bound the entire turn. The default is long
                # enough for max-effort reasoning and configurable per run.
                turn_timeout = float(os.environ.get("GPT2AGENT_TURN_TIMEOUT_SECONDS", "900"))
                text = await asyncio.wait_for(conv.complete(
                    config.model,
                    [{"role": "user", "content": prompt}],
                    temporary=config.temporary,
                    thinking_effort=config.thinking_effort,
                    attachments=attachment_descs,
                    pre_submit=at_conversation_submit,
                ), timeout=max(turn_timeout, 1.0))
            except asyncio.TimeoutError as exc:
                message = (
                    "gpt2agent turn exceeded the outer deadline "
                    f"({max(turn_timeout, 1.0):g}s); websocket task was cancelled "
                    "and the backend scope is being closed"
                )
                # A timeout before the submit hook is safe to retry. Once the
                # hook ran, server-side turn state is ambiguous; callers must
                # not blindly submit the same intellectual request again.
                if not pre_submit_completed:
                    raise ProviderError(message, retryable=True, ambiguous=False, phase=ProviderPhase.PRE_SUBMIT) from exc
                raise ProviderError(message, retryable=False, ambiguous=True, phase=ProviderPhase.POST_SUBMIT_AMBIGUOUS) from exc
            except Exception as exc:
                if not pre_submit_completed:
                    raise ProviderError(
                        f"gpt2agent pre-submit launch gate failed: {exc}",
                        phase=ProviderPhase.PRE_SUBMIT,
                    ) from exc
                # Stream dropped after the POST. The turn may still be running /
                # already finished on the server. Never blindly re-submit: try to
                # recover the already-live turn's artifact bytes before failing.
                recovered = None
                # Temporary chats are intentionally absent from history. Never
                # probe /conversations or /conversation/{id} to recover them:
                # repeated history polling is account-protection pressure and
                # cannot recover a temporary turn anyway.
                if not config.temporary:
                    try:
                        recovered = await asyncio.to_thread(
                            self._recover_submitted_turn_artifact, backend, prompt
                        )
                    except Exception:
                        recovered = None
                if recovered is not None:
                    return ChatJobResult(
                        text="(recovered from already-live turn)",
                        artifact_bytes=recovered["bytes"],
                        artifact_name=recovered["name"],
                        meta={
                            "conversation_id": recovered.get("conversation_id"),
                            "message_id": recovered.get("message_id"),
                            "sandbox_path": recovered.get("sandbox_path"),
                            "recovered_after_ambiguous_submit": True,
                        },
                    )
                raise ProviderError(
                    f"gpt2agent chat failed: {exc}",
                    phase=classify_conversation_phase(exc),
                ) from exc

            turn_meta = dict(getattr(conv, "last_turn_metadata", {}) or {})
            result_meta = {
                key: turn_meta.get(key)
                for key in ("conversation_id", "message_id", "temporary")
                if turn_meta.get(key) is not None
            }
            if submitted_at is not None:
                result_meta["turn_elapsed_seconds"] = max(
                    0.0, self._clock() - submitted_at
                )
            model_slugs = _resolved_model_slugs(turn_meta)
            if model_slugs:
                result_meta["resolved_model_slugs"] = model_slugs
            # Detect an implausibly fast heavy-reasoning result before artifact
            # handling.  Previously a fallback response that omitted the ZIP
            # raised "missing artifact" first and bypassed the account-safety
            # circuit entirely.
            elapsed = result_meta.get("turn_elapsed_seconds")
            if (
                self._suspected_fallback_seconds > 0
                and isinstance(elapsed, (int, float))
                and elapsed < self._suspected_fallback_seconds
            ):
                self._account_protection_latch = "suspected model fallback after submit"
                signature = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
                raise ProviderError(
                    "suspected ChatGPT fallback; result rejected and further account calls "
                    "latched for this process: "
                    f"turn completed in {elapsed:.3f}s, below the "
                    f"{self._suspected_fallback_seconds:.3f}s floor; "
                    f"result_text_sha256={signature}",
                    retryable=False,
                    ambiguous=False,
                    phase=ProviderPhase.POST_SUBMIT,
                )
            # Without an artifact expectation, a completed turn returns immediately and never waits for or fails over a missing artifact.
            if not expect_artifact:
                return ChatJobResult(text=text or "", meta=result_meta)

            try:
                artifact = await asyncio.to_thread(
                    self._download_live_turn_artifact, text, backend, turn_meta
                )
            except Exception as exc:
                raise ProviderError(
                    f"submitted ChatGPT artifact download failed: {exc}",
                    retryable=False,
                    ambiguous=True,
                ) from exc

            if artifact is None and config.temporary:
                try:
                    json.loads(text or "")
                except (TypeError, json.JSONDecodeError):
                    pass
                else:
                    return ChatJobResult(
                        text=text or "",
                        artifact_bytes=(text or "").encode("utf-8"),
                        artifact_name="turn_artifact.json",
                        meta={**result_meta, "artifact_from_json_text": True},
                    )
                if not turn_meta.get("conversation_id") or not turn_meta.get("message_id"):
                    raise ProviderError(
                        "submitted ChatGPT job has no live-turn message metadata; "
                        "refusing automatic re-submit",
                        retryable=False,
                        ambiguous=True,
                    )
                if not self._collect_live_turn_links(text or "", turn_meta):
                    raise ProviderError(
                        "submitted ChatGPT job has no downloadable artifact link; "
                        "refusing automatic re-submit",
                        retryable=False,
                        ambiguous=True,
                    )
                try:
                    artifact = await self._wait_for_live_turn_artifact(
                        text, backend, turn_meta
                    )
                except Exception as exc:
                    raise ProviderError(
                        f"submitted ChatGPT artifact download failed: {exc}",
                        retryable=False,
                        ambiguous=True,
                    ) from exc
            if artifact is None and not config.temporary:
                artifact = await asyncio.to_thread(
                    self._download_first_artifact, text, backend
                )
            if artifact is None and not config.temporary:
                artifact = await asyncio.to_thread(
                    self._wait_for_submitted_artifact,
                    prompt,
                    backend,
                    known_conversations,
                )
            if artifact is None:
                raise ProviderError(
                    "submitted ChatGPT job did not finalize a downloadable artifact "
                    "within the collection window; refusing automatic re-submit",
                    retryable=False,
                    ambiguous=True,
                )
            return ChatJobResult(
                text=text or "",
                artifact_bytes=artifact["bytes"],
                artifact_name=artifact["name"],
                meta={
                    **result_meta,
                    "conversation_id": artifact.get("conversation_id"),
                    "message_id": artifact.get("message_id"),
                    "sandbox_path": artifact.get("sandbox_path"),
                },
            )

    async def _wait_for_live_turn_artifact(
        self, response_text: str, backend: Any, turn_meta: dict
    ) -> dict | None:
        timeout = float(os.environ.get("GPT2AGENT_ARTIFACT_TIMEOUT_SECONDS", "900"))
        interval = float(os.environ.get("GPT2AGENT_ARTIFACT_POLL_SECONDS", "10"))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(max(interval, 0), remaining))
            artifact = await asyncio.to_thread(
                self._download_live_turn_artifact, response_text, backend, turn_meta
            )
            if artifact is not None:
                return artifact

    @staticmethod
    def _collect_live_turn_links(
        response_text: str, turn_meta: dict
    ) -> list[tuple[str, str]]:
        """Collect (message_id, sandbox_path) candidates for this live turn.

        Sandbox links can appear either in the streamed assistant text or in the
        captured assistant/tool message payloads (``last_turn_metadata``) when the
        final text does not echo the ``sandbox:`` link. We resolve both so the
        artifact of an already-submitted temporary turn is found without a resubmit.
        """
        fallback_message_id = turn_meta.get("message_id")
        candidates: list[tuple[str, str]] = []
        for sandbox_path in reversed(_SANDBOX_LINK_RE.findall(response_text or "")):
            candidates.append((fallback_message_id, sandbox_path))
        raw_messages = list(turn_meta.get("messages") or [])
        if turn_meta.get("message") is not None:
            raw_messages.append(turn_meta["message"])
        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id") or fallback_message_id
            parts = (msg.get("content") or {}).get("parts") or []
            for part in parts:
                if not isinstance(part, str):
                    continue
                for sandbox_path in _SANDBOX_LINK_RE.findall(part):
                    candidates.append((msg_id, sandbox_path))
        unique: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for msg_id, sandbox_path in candidates:
            if not msg_id:
                continue
            key = (msg_id, sandbox_path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(key)
        return unique

    def _download_live_turn_artifact(
        self, response_text: str, backend: Any, turn_meta: dict
    ) -> dict | None:
        conversation_id = turn_meta.get("conversation_id")
        candidates = self._collect_live_turn_links(response_text, turn_meta)
        if not conversation_id or not candidates:
            return None
        for message_id, sandbox_path in candidates:
            try:
                query = urlencode(
                    {"message_id": message_id, "sandbox_path": sandbox_path}
                )
                resolved = backend.get(
                    f"/backend-api/conversation/{conversation_id}/interpreter/download?{query}"
                )
                download_url = (resolved or {}).get("download_url")
                if not download_url:
                    continue
                response = backend._session.get(download_url, timeout=120)
                response.raise_for_status()
            except Exception as exc:
                _log.warning("live-turn artifact download failed: %s", exc)
                continue
            return {
                "bytes": response.content,
                "name": Path(sandbox_path).name or "artifact.bin",
                "conversation_id": conversation_id,
                "message_id": message_id,
                "sandbox_path": sandbox_path,
            }
        return None

    @staticmethod
    def _recent_conversation_ids(backend: Any) -> set[str]:
        try:
            recent = backend.get(
                "/backend-api/conversations?offset=0&limit=100&order=updated"
            ) or {}
        except Exception:
            return set()
        return {
            item["id"]
            for item in (recent.get("items") or [])
            if item.get("id")
        }

    def _wait_for_submitted_artifact(
        self,
        prompt: str,
        backend: Any,
        known_conversations: set[str],
    ) -> dict | None:
        timeout = float(os.environ.get("GPT2AGENT_ARTIFACT_TIMEOUT_SECONDS", "900"))
        interval = float(os.environ.get("GPT2AGENT_ARTIFACT_POLL_SECONDS", "10"))
        loop = asyncio.new_event_loop()
        try:
            deadline = loop.time() + timeout
            while True:
                try:
                    recent = backend.get(
                        "/backend-api/conversations?offset=0&limit=100&order=updated"
                    ) or {}
                except Exception:
                    recent = {}
                candidates = [
                    item.get("id")
                    for item in (recent.get("items") or [])
                    if item.get("id") and item.get("id") not in known_conversations
                ]
                for conversation_id in candidates:
                    try:
                        detail = backend.get(
                            f"/backend-api/conversation/{conversation_id}"
                        )
                    except Exception:
                        continue
                    if not self._conversation_contains_prompt(detail, prompt):
                        continue
                    artifact = self._download_artifact_from_detail(
                        backend, conversation_id, detail
                    )
                    if artifact is not None:
                        return artifact
                if loop.time() >= deadline:
                    return None
                import time

                time.sleep(interval)
        finally:
            loop.close()

    @staticmethod
    def _conversation_contains_prompt(detail: dict, prompt: str) -> bool:
        marker = prompt[:256]
        for node in (detail.get("mapping") or {}).values():
            msg = (node or {}).get("message") or {}
            if (msg.get("author") or {}).get("role") != "user":
                continue
            for part in (msg.get("content") or {}).get("parts") or []:
                if isinstance(part, str) and marker in part:
                    return True
        return False

    def _download_artifact_from_detail(
        self, backend: Any, conversation_id: str, detail: dict
    ) -> dict | None:
        links: list[str] = []
        for node in (detail.get("mapping") or {}).values():
            msg = (node or {}).get("message") or {}
            if (msg.get("author") or {}).get("role") != "assistant":
                continue
            for part in (msg.get("content") or {}).get("parts") or []:
                if isinstance(part, str):
                    links.extend(_SANDBOX_LINK_RE.findall(part))
        for sandbox_path in reversed(links):
            match = self._find_message(sandbox_path, detail)
            if match is None:
                continue
            message_id, sandbox_path = match
            try:
                query = urlencode(
                    {"message_id": message_id, "sandbox_path": sandbox_path}
                )
                resolved = backend.get(
                    f"/backend-api/conversation/{conversation_id}/interpreter/download?{query}"
                )
                download_url = (resolved or {}).get("download_url")
                if not download_url:
                    continue
                response = backend._session.get(download_url, timeout=120)
                response.raise_for_status()
            except Exception:
                continue
            return {
                "bytes": response.content,
                "name": Path(sandbox_path).name or "artifact.bin",
                "conversation_id": conversation_id,
                "sandbox_path": sandbox_path,
            }
        return None

    @staticmethod
    def _attachment_cache_key(
        path: Path, temporary: bool
    ) -> tuple[str, int, str, bool]:
        if not path.is_file():
            raise ValueError(f"attachment does not exist or is not a file: {path}")
        size = path.stat().st_size
        if size == 0:
            raise ValueError("attachment must not be empty")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return path.name, size, digest.hexdigest(), temporary

    async def _shared_attachment_descriptor(
        self, conv: Any, backend: Any, path: Path, *, temporary: bool
    ) -> dict:
        """Upload identical attachment bytes once per provider process.

        The task table is a single-flight guard: concurrent lanes awaiting the
        same evidence file receive independent descriptor dictionaries backed
        by one physical upload.  Failed uploads are never cached.
        """
        key = await asyncio.to_thread(self._attachment_cache_key, path, temporary)
        cached = self._attachment_cache.get(key)
        if cached is not None:
            return dict(cached)

        task = self._attachment_uploads.get(key)
        if task is None:
            task = asyncio.create_task(
                self._upload_attachment(conv, backend, path, temporary=temporary)
            )
            self._attachment_uploads[key] = task

        try:
            descriptor = await asyncio.shield(task)
        except BaseException:
            if task.done() and self._attachment_uploads.get(key) is task:
                self._attachment_uploads.pop(key, None)
            raise

        if self._attachment_uploads.get(key) is task:
            self._attachment_uploads.pop(key, None)
            self._attachment_cache[key] = dict(descriptor)
        return dict(descriptor)

    async def _upload_attachment(
        self, conv: Any, backend: Any, path: Path, *, temporary: bool
    ) -> dict:
        # ChatGPT's retrieval index rejects ZIP archives with
        # file.indexing.error. Package jobs still need the exact ZIP bytes as
        # a code-interpreter attachment, so upload archives without indexing.
        if path.suffix.lower() == ".zip":
            return await asyncio.to_thread(
                self._upload_unindexed_file_sync, backend, path, temporary
            )
        try:
            return await conv.upload_attachment(path, temporary=temporary)
        except Exception as exc:
            # Indexing is optional for code-interpreter attachments.  Preserve
            # exact file bytes and retry only the upload phase without retrieval
            # indexing; no intellectual request has been submitted at this point.
            if "file.indexing.error" not in str(exc).lower():
                raise
            _log.warning(
                "retrieval indexing failed for %s; retrying unindexed upload",
                path.name,
            )
            return await asyncio.to_thread(
                self._upload_unindexed_file_sync, backend, path, temporary
            )

    def _upload_unindexed_archive_sync(
        self, backend: Any, path: Path, temporary: bool
    ) -> dict:
        """Compatibility wrapper for callers of the original ZIP-only helper."""
        return self._upload_unindexed_file_sync(backend, path, temporary)

    def _upload_unindexed_file_sync(
        self, backend: Any, path: Path, temporary: bool
    ) -> dict:
        if not path.is_file():
            raise ValueError(f"attachment does not exist or is not a file: {path}")
        size = path.stat().st_size
        if size == 0:
            raise ValueError("attachment must not be empty")
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
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
            "is_project_thread": False,
        }
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
                        "metadata": json.dumps(metadata),
                    },
                    timeout=300,
                )
            elif route == "upload_content_bytes":
                uploaded = session.post(
                    destination,
                    files=files,
                    data={"upload_url": upload_url},
                    timeout=300,
                )
                if uploaded.status_code not in (200, 201, 204):
                    raise RuntimeError(
                        f"file byte upload failed ({uploaded.status_code})"
                    )
                response = session.post(
                    _BASE + "/backend-api/files/process_upload_stream",
                    headers={"Content-Type": "application/json"},
                    json=processing,
                    timeout=300,
                )
            else:
                direct_headers = {"Content-Type": mime_type}
                if "x-amz-algorithm" not in destination.lower():
                    direct_headers.update(
                        {
                            "x-ms-blob-type": "BlockBlob",
                            "x-ms-version": "2020-04-08",
                        }
                    )
                uploaded = self._sse_mod.curl_requests.put(
                    destination,
                    headers=direct_headers,
                    data=path.read_bytes(),
                    impersonate="chrome131",
                    verify=True,
                    timeout=300,
                )
                if uploaded.status_code not in (200, 201, 202, 204):
                    raise RuntimeError(
                        f"direct file upload failed ({uploaded.status_code})"
                    )
                response = session.post(
                    _BASE + "/backend-api/files/process_upload_stream",
                    headers={"Content-Type": "application/json"},
                    json=processing,
                    timeout=300,
                )
        if response.status_code not in (200, 201):
            raise RuntimeError(f"file processing failed ({response.status_code})")
        for raw_line in response.text.splitlines():
            line = raw_line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_name = str(event.get("event") or "")
            if event_name.rsplit(".", 1)[-1] in {
                "error", "cancelled", "failed", "unknown"
            }:
                raise RuntimeError(f"file processing failed: {event_name}")
        return {
            "id": file_id,
            "size": size,
            "name": path.name,
            "mime_type": mime_type,
            "file_token_size": None,
            "source": "local",
            "is_big_paste": False,
        }

    # -- sandbox artifact read path (transport-accepted, see parity report §6) --

    def _download_first_artifact(self, response_text: str, backend: Any) -> dict | None:
        links = _SANDBOX_LINK_RE.findall(response_text or "")
        if not links:
            _log.warning("no sandbox artifact link found in assistant response")
            return None
        try:
            recent = backend.get("/backend-api/conversations?offset=0&limit=50&order=updated") or {}
        except Exception:
            recent = {}
        conv_ids = [item.get("id") for item in (recent.get("items") or []) if item.get("id")]
        for link in links:
            for conv_id in conv_ids:
                try:
                    detail = backend.get(f"/backend-api/conversation/{conv_id}")
                except Exception:
                    continue
                match = self._find_message(link, detail)
                if match is None:
                    continue
                message_id, sandbox_path = match
                try:
                    query = urlencode({"message_id": message_id, "sandbox_path": sandbox_path})
                    resolved = backend.get(
                        f"/backend-api/conversation/{conv_id}/interpreter/download?{query}"
                    )
                except Exception as exc:
                    _log.warning("interpreter/download failed for %s: %s", link, exc)
                    continue
                download_url = (resolved or {}).get("download_url")
                if not download_url:
                    continue
                try:
                    resp = backend._session.get(download_url, timeout=120)
                    resp.raise_for_status()
                except Exception as exc:
                    _log.warning("artifact download failed: %s", exc)
                    continue
                name = Path(sandbox_path).name or "artifact.bin"
                return {
                    "bytes": resp.content,
                    "name": name,
                    "conversation_id": conv_id,
                    "sandbox_path": sandbox_path,
                }
        _log.warning("no downloadable sandbox artifact resolved")
        return None

    def _recover_submitted_turn_artifact(self, backend: Any, prompt: str) -> dict | None:
        """Recover an already-submitted turn's artifact without a new submit.

        Temporary chats are intentionally absent from conversation history, so
        only live turns (within the collection window) that reference the exact
        prompt prefix are inspected.
        """
        known = self._recent_conversation_ids(backend)
        detail = self._wait_for_submitted_artifact(prompt, backend, known)
        if detail is None:
            return None
        return detail

    @staticmethod
    def _find_message(sandbox_path: str, detail: dict) -> tuple[str, str] | None:
        for node in (detail.get("mapping") or {}).values():
            msg = (node or {}).get("message") or {}
            if (msg.get("author") or {}).get("role") != "assistant":
                continue
            parts = (msg.get("content") or {}).get("parts") or []
            for part in parts:
                if not isinstance(part, str):
                    continue
                if f"sandbox:{sandbox_path}" in part:
                    return msg.get("id"), sandbox_path
        return None
