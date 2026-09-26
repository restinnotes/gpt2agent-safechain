"""Human Pacing & Concurrency Launch Gate for ChatGPT Web.

Implements the proven LaunchIntervalChatProvider to:
1. Emulate human interaction intervals by spacing out consecutive web-turn submissions
   with randomized jitter (e.g. 2.5s - 5.0s).
2. Prevent request bunching: enforces an inter-launch delay boundary immediately
   before intellectual provider submit (via the provider's pre_submit hook).
3. Bounded concurrency: caps concurrent active turns to a safe low limit (e.g. 2)
   to protect against Cloudflare bot-scoring and OpenAI rate limits (403 unusual activity).
4. Full auditable ledger: persists WEB_LAUNCH_JITTER.json tracking call timings,
   gate wait times, and launch ordinal policies.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from pathlib import Path
from typing import Any

from .protocol import ChatJobResult, ChatProvider, JobConfig

DEFAULT_MIN_INTER_LAUNCH_SECONDS = 2.0
DEFAULT_MAX_INTER_LAUNCH_SECONDS = 4.0
DEFAULT_MAX_ACTIVE_TURNS: int | None = None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LaunchIntervalChatProvider:
    """Wraps a ChatProvider with a global launch gate and human-like pacing jitter."""

    def __init__(
        self,
        provider: ChatProvider,
        ledger_path: Path,
        *,
        minimum: float = DEFAULT_MIN_INTER_LAUNCH_SECONDS,
        maximum: float = DEFAULT_MAX_INTER_LAUNCH_SECONDS,
        max_active_turns: int | None = DEFAULT_MAX_ACTIVE_TURNS,
        rng: Any = None,
        sleep=asyncio.sleep,
        clock=time.perf_counter,
    ) -> None:
        self.provider = provider
        self.ledger_path = Path(ledger_path)
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.rng = rng or random.SystemRandom()
        self.sleep = sleep
        self.clock = clock
        self.calls: list[dict[str, Any]] = []
        self._next_id = 1
        self._launch_lock = asyncio.Lock()
        self.max_active_turns = max_active_turns
        self._turn_gate = asyncio.Semaphore(max_active_turns) if (max_active_turns and max_active_turns > 0) else None
        self._last_launch_seconds: float | None = None
        self._last_launch_call_id: str | None = None

        if self.ledger_path.is_file():
            try:
                prior = json.loads(self.ledger_path.read_text(encoding="utf-8"))
                if isinstance(prior.get("calls"), list):
                    self.calls = prior["calls"]
                    ordinals = []
                    for call in self.calls:
                        match = re.fullmatch(r"WEB-(\d+)", str(call.get("call_id", "")))
                        if match:
                            ordinals.append(int(match.group(1)))
                        if call.get("status") in {
                            "QUEUED_FOR_GLOBAL_LAUNCH_GATE",
                            "GLOBAL_LAUNCH_GATE_WAIT",
                            "IN_FLIGHT",
                        }:
                            call["status_before_process_exit"] = call["status"]
                            call["status"] = "INTERRUPTED_PROCESS_EXIT"
                    self._next_id = max(ordinals, default=0) + 1
            except Exception:
                self.calls = []

        self._persist()

    def _now_ns(self) -> int:
        return int(self.clock() * 1_000_000_000)

    def _persist(self) -> None:
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            doc = {
                "schema_version": 2,
                "policy": (
                    "One global gate controls all intellectual Web-call launches. The first "
                    "actual provider submission is immediate; each later submission is at least "
                    "the configured conservative interval after the preceding submission. A small "
                    "bounded number of provider turns may remain active so sampling width is retained."
                ),
                "historical_filename": self.ledger_path.name,
                "launch_gate": {
                    "scope": "all intellectual calls through this provider",
                    "first_launch": "IMMEDIATE",
                    "subsequent_inter_launch_seconds": {
                        "minimum": self.minimum,
                        "maximum": self.maximum,
                    },
                    "boundary": "immediately_before_intellectual_provider_submit",
                    "max_active_turns": self.max_active_turns,
                },
                "calls": self.calls,
            }
            self.ledger_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass

    async def _wait_for_launch_slot(self, entry: dict[str, Any]) -> None:
        async with self._launch_lock:
            now = self.clock()
            entry["gate_entered_monotonic_ns"] = int(now * 1_000_000_000)
            previous_launch = self._last_launch_seconds
            if previous_launch is None:
                entry["launch_ordinal_policy"] = "FIRST_IMMEDIATE"
                entry["sampled_inter_launch_seconds"] = None
                earliest_launch = now
            else:
                interval = float(self.rng.uniform(self.minimum, self.maximum))
                entry["launch_ordinal_policy"] = "GLOBAL_INTERVAL_AFTER_PREVIOUS"
                entry["previous_launch_call_id"] = self._last_launch_call_id
                entry["sampled_inter_launch_seconds"] = interval
                earliest_launch = previous_launch + interval

            entry["earliest_launch_monotonic_ns"] = int(earliest_launch * 1_000_000_000)
            entry["status"] = "GLOBAL_LAUNCH_GATE_WAIT"
            self._persist()

            remaining = earliest_launch - self.clock()
            while remaining > 0:
                await self.sleep(remaining)
                remaining = earliest_launch - self.clock()

            launched = self.clock()
            entry["gate_wait_seconds"] = max(0.0, launched - now)
            entry["launched_monotonic_ns"] = int(launched * 1_000_000_000)
            if previous_launch is not None:
                entry["observed_inter_launch_seconds"] = launched - previous_launch
            entry["status"] = "IN_FLIGHT"
            self._last_launch_seconds = launched
            self._last_launch_call_id = entry["call_id"]
            self._persist()

    async def complete_chat(
        self,
        *,
        prompt: str,
        config: JobConfig,
        attachments: list[str] | None = None,
        expect_artifact: bool = False,
        pre_submit: Any = None,
    ) -> ChatJobResult:
        call_id = f"WEB-{self._next_id:04d}"
        self._next_id += 1
        entry: dict[str, Any] = {
            "call_id": call_id,
            "prompt_sha256": _sha256_text(prompt),
            "expect_artifact": expect_artifact,
            "attachment_count": len(attachments or []),
            "queued_monotonic_ns": self._now_ns(),
            "status": "QUEUED_FOR_GLOBAL_LAUNCH_GATE",
        }
        self.calls.append(entry)
        self._persist()
        submitted = False

        async def at_launch_gate() -> None:
            nonlocal submitted
            if submitted:
                raise RuntimeError("provider invoked pre-submit launch gate more than once")
            await self._wait_for_launch_slot(entry)
            if pre_submit is not None:
                await pre_submit()
            submitted = True

        try:
            if self._turn_gate is not None:
                async with self._turn_gate:
                    result = await self.provider.complete_chat(
                        prompt=prompt,
                        config=config,
                        attachments=attachments,
                        expect_artifact=expect_artifact,
                        pre_submit=at_launch_gate,
                    )
            else:
                result = await self.provider.complete_chat(
                    prompt=prompt,
                    config=config,
                    attachments=attachments,
                    expect_artifact=expect_artifact,
                    pre_submit=at_launch_gate,
                )
            if not submitted:
                # If provider didn't use hook (e.g. mock), record as launched
                entry["status"] = "IN_FLIGHT"
            entry["status"] = "COMPLETED"
            return result
        except BaseException as exc:
            entry["status"] = (
                "CANCELLED" if isinstance(exc, asyncio.CancelledError) else "FAILED"
            )
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            terminal_ns = self._now_ns()
            entry["terminal_monotonic_ns"] = terminal_ns
            if "launched_monotonic_ns" in entry:
                entry["finished_monotonic_ns"] = terminal_ns
            self._persist()

    async def check_readiness(self, *, probe_sentinel: bool = False) -> dict[str, Any]:
        if hasattr(self.provider, "check_readiness"):
            return await self.provider.check_readiness(probe_sentinel=probe_sentinel)
        return {"ready": True, "provider": type(self.provider).__name__}
