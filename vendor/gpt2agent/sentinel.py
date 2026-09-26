"""Sentinel gate: fetch chat-requirements via the 2026 two-call protocol (no turnstile VM)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from curl_cffi.requests import AsyncSession

from gpt2agent._log_redact import redact_error as _redact_error
from gpt2agent._vendored import pow as _pow

if TYPE_CHECKING:
    from gpt2agent.backend import BackendClient

_log = logging.getLogger(__name__)

_CHAT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Serializes ONLY token acquisition. chat-requirements tokens are single-use
# (a reused token 403s on its second /conversation POST), so we never share a
# token between turns — but concurrent turns firing prepare+finalize at once
# spikes the sentinel endpoints and trips bot-scoring. One-at-a-time fetch
# keeps bursts flat while every turn still gets its own fresh token.
_TOKEN_FETCH_LOCK = asyncio.Lock()


class SentinelGate:
    def __init__(self, backend: "BackendClient") -> None:
        self._backend = backend

    async def get_tokens(self) -> dict[str, str]:
        """Fetch chat-requirements via the 2026 two-call sentinel protocol.

        POST /backend-api/sentinel/chat-requirements/prepare  {"p": p}
          -> {prepare_token, proofofwork{required,seed,difficulty}, turnstile, so}
        solve PoW locally
        POST /backend-api/sentinel/chat-requirements/finalize
          -> {token, persona, expire_after, expire_at}

        The finalize-issued token works WITHOUT solving the turnstile dx VM
        challenge (verified live 2026-08-18), so the vendored turnstile solver
        is not consulted here. A fresh token is fetched per turn: chat-requirements
        tokens are single-use (a cached token 403s on its second /conversation
        POST — verified live), which is also exactly what the web does (one
        prepare/finalize per send).
        """
        async with _TOKEN_FETCH_LOCK:
            last_err: Exception | None = None
            for attempt in range(1, 4):
                try:
                    out, _ = await self._fetch_tokens()
                    return out
                except RuntimeError as e:
                    last_err = e
                    if any(code in str(e) for code in ("522", "504", "502", "503")) and attempt < 3:
                        await asyncio.sleep(2.0 * attempt)
                        continue
                    raise
            if last_err:
                raise last_err

    async def _fetch_tokens(self) -> tuple[dict[str, str], int | None]:
        headers = dict(self._backend._session.headers)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "*/*"

        ua = headers.get("User-Agent") or _CHAT_UA
        p = _pow.get_requirements_token(ua)

        url = "https://chatgpt.com/backend-api/sentinel/chat-requirements"

        async with AsyncSession(impersonate="chrome131", verify=True) as s:
            r = await s.post(url + "/prepare", headers=headers, json={"p": p}, timeout=30)

            if r.status_code != 200:
                body = r.text if hasattr(r, "text") else str(r.content)
                raise RuntimeError(
                    f"sentinel/chat-requirements/prepare HTTP {r.status_code}: "
                    f"{_redact_error(body)}"
                )

            try:
                resp = r.json()
            except Exception as exc:
                body = r.text if hasattr(r, "text") else str(r.content)
                raise RuntimeError(
                    f"sentinel/chat-requirements non-JSON 200: {_redact_error(body)}"
                ) from exc
            if not isinstance(resp, dict):
                raise RuntimeError(
                    "sentinel/chat-requirements unexpected response shape: "
                    f"{_redact_error(json.dumps(resp, ensure_ascii=False))}"
                )
            prepare_token = resp.get("prepare_token")
            if not prepare_token:
                raise RuntimeError(
                    "sentinel/chat-requirements/prepare no prepare_token: "
                    f"{_redact_error(json.dumps(resp, ensure_ascii=False))}"
                )

            out: dict[str, str] = {"chat-requirements": "", "proof": ""}

            pow_block = resp.get("proofofwork") or {}
            if pow_block.get("required"):
                seed = pow_block.get("seed")
                diff = pow_block.get("difficulty")
                if not seed or not diff:
                    raise RuntimeError(f"sentinel POW missing seed/difficulty: {pow_block}")
                proof = await asyncio.to_thread(_pow.solve_pow, seed, diff, ua)
                if not proof:
                    raise RuntimeError("required POW challenge could not be solved")
                out["proof"] = proof
            else:
                out["proof"] = ""

            finalize: dict = {"prepare_token": prepare_token}
            if out["proof"]:
                finalize["proofofwork"] = out["proof"]
            r2 = await s.post(url + "/finalize", headers=headers, json=finalize, timeout=30)
        if r2.status_code != 200:
            body = r2.text if hasattr(r2, "text") else str(r2.content)
            raise RuntimeError(
                f"sentinel/chat-requirements/finalize HTTP {r2.status_code}: "
                f"{_redact_error(body)}"
            )
        try:
            fin = r2.json()
        except Exception as exc:
            body = r2.text if hasattr(r2, "text") else str(r2.content)
            raise RuntimeError(
                f"sentinel/chat-requirements/finalize non-JSON 200: {_redact_error(body)}"
            ) from exc
        chat_token = fin.get("token") if isinstance(fin, dict) else None
        if not chat_token:
            raise RuntimeError(
                "sentinel/chat-requirements/finalize no token: "
                f"{_redact_error(json.dumps(fin, ensure_ascii=False))}"
            )
        out["chat-requirements"] = chat_token

        expire_at_ms: int | None = None
        if isinstance(fin, dict):
            raw = fin.get("expire_at")
            if isinstance(raw, (int, float)):
                # expire_at is epoch SECONDS; align with the ms clock used by
                # _cached_tokens_valid (time.time() * 1000).
                expire_at_ms = int(raw * 1000)
            elif isinstance(raw, str) and raw.isdigit():
                expire_at_ms = int(raw) * 1000
            raw_after = fin.get("expire_after")
            if expire_at_ms is None and isinstance(raw_after, (int, float)):
                expire_at_ms = int((time.time() + raw_after) * 1000)
        # Fernet tokens carry no JWT `exp` (this isn't a JWT); fall back to a
        # bounded reuse window (5 min) so concurrent calls share one token
        # without ever reusing a token for much longer than the server allows.
        if expire_at_ms is None:
            expire_at_ms = int((time.time() + 300) * 1000)
        return out, expire_at_ms
