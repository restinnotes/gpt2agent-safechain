"""Account-scoped state and transport lifetime. No geo lookup or identity rotation."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookiejar import Cookie
from pathlib import Path
from zoneinfo import ZoneInfo

from curl_cffi.requests import AsyncSession

IMPERSONATE = "chrome136"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
SEC_CH_UA = '"Chromium";v="136", "Not=A?Brand";v="24", "Google Chrome";v="136"'


def account_key(token: str, source: Path | None) -> str:
    # JWT claims are used ONLY to partition local state, never for authorization.
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        subject = claims["sub"]
        account = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id", "")
        identity = f"{subject}:{account}"
    except (ValueError, KeyError, IndexError, TypeError):
        # Opaque credentials cannot safely share cookies after a token change.
        identity = hashlib.sha256(token.encode()).hexdigest()
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def _atomic_json(path: Path, value: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".runtime-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _try_lock(stream) -> bool:
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, BlockingIOError):
        return False


def _unlock(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_UN)


class AccountRuntime:
    def __init__(self, token: str, source: Path | None) -> None:
        self.key = account_key(token, source)
        root = Path(os.environ.get("GPT2AGENT_RUNTIME_DIR", str(Path.home() / ".gpt2agent" / "accounts")))
        self.directory = root / self.key
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "runtime.json"
        self._lock_stream = open(self.directory / "identity.lock", "a+b")
        self._lock_stream.write(b"\0")
        self._lock_stream.flush()
        # Identity initialization is atomic even when two processes start together.
        deadline = time.monotonic() + 5
        while not _try_lock(self._lock_stream):
            if time.monotonic() >= deadline:
                self._lock_stream.close()
                raise RuntimeError("account runtime busy; reuse the existing account worker")
            time.sleep(0.05)
        try:
            state = self._read()
            if self.path.exists() and not all(state.get(key) for key in (
                    "device_id", "session_id", "timezone", "locale", "client_version", "client_build")):
                raise RuntimeError("incomplete account runtime state; refusing to rotate identity")
            self.device_id = state.get("device_id") or os.environ.get("GPT2AGENT_DEVICE_ID") or str(uuid.uuid4())
            self.session_id = state.get("session_id") or str(uuid.uuid4())
            uuid.UUID(self.device_id)
            uuid.UUID(self.session_id)
            self.timezone = state.get("timezone") or os.environ.get("GPT2AGENT_TIMEZONE", "UTC")
            ZoneInfo(self.timezone)  # Reject invalid config rather than silently fabricate an offset.
            self.locale = state.get("locale") or os.environ.get("GPT2AGENT_LOCALE", "en-US")
            self.client_version = state.get("client_version") or os.environ.get("GPT2AGENT_CLIENT_VERSION", "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad")
            self.client_build = state.get("client_build") or os.environ.get("GPT2AGENT_CLIENT_BUILD", "5955942")
            state.update(device_id=self.device_id, session_id=self.session_id, timezone=self.timezone,
                         locale=self.locale, client_version=self.client_version, client_build=self.client_build)
            if not self.path.exists():
                _atomic_json(self.path, state)
        finally:
            _unlock(self._lock_stream)
            self._lock_stream.close()
        self._lock_stream = open(self.directory / "account.lock", "a+b")
        self._lock_stream.write(b"\0")
        self._lock_stream.flush()
        self._async_session = None
        self._loop = None
        self._gate = asyncio.Lock()
        self._warmed = False
        self._loaded_at = time.time()

    def requirements_config(self, ua: str) -> list:
        """Reuse the existing PoW wire format without its per-call identity jitter.

        Screen/memory values are compatibility constants from the legacy helper,
        not measured browser telemetry. Nonce/time still change per acquisition.
        """
        now = datetime.now(ZoneInfo(self.timezone))
        date = now.strftime("%a %b %d %Y %H:%M:%S GMT%z") + f" ({self.timezone})"
        elapsed_ms = max(0, (time.time() - self._loaded_at) * 1000)
        languages = self.locale + "," + self.locale.split("-")[0]
        return [3000, date, 4294705152, 0, ua, "", self.client_version,
                self.locale, languages, 0, "language−" + self.locale, "location",
                "window", elapsed_ms, self.session_id, "", 8, self._loaded_at * 1000]

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("invalid account runtime state; refusing to rotate identity")
        return data

    @property
    def timezone_offset_min(self) -> int:
        offset = datetime.now(ZoneInfo(self.timezone)).utcoffset()
        return int(-offset.total_seconds() / 60) if offset else 0

    def check_backoff(self) -> None:
        remaining = float(self._read().get("backoff_until", 0)) - time.time()
        if remaining > 0:
            raise RuntimeError(f"account protection backoff active for {remaining:.0f}s; no request sent")

    def note_response(self, response) -> None:
        if response.status_code not in (403, 429):
            return
        delay = 900.0 if response.status_code == 403 else 60.0
        raw = response.headers.get("Retry-After", "")
        try:
            delay = max(delay, float(raw))
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delay = max(delay, date.timestamp() - time.time())
            except (ValueError, TypeError):
                pass
        self.backoff(delay)

    def backoff(self, delay: float = 900) -> None:
        state = self._read()
        state["backoff_until"] = max(float(state.get("backoff_until", 0)), time.time() + delay)
        _atomic_json(self.path, state)

    def load_cookies(self, session) -> None:
        session.cookies.clear()
        for fields in self._read().get("cookies", []):
            cookie = Cookie(**fields)
            if not cookie.is_expired() and (cookie.domain == "chatgpt.com" or cookie.domain.endswith(".chatgpt.com")):
                session.cookies.jar.set_cookie(cookie)

    def save_cookies(self, session) -> None:
        fields = ("version", "name", "value", "port", "port_specified", "domain", "domain_specified",
                  "domain_initial_dot", "path", "path_specified", "secure", "expires", "discard",
                  "comment", "comment_url", "rfc2109")
        cookies = []
        for cookie in session.cookies.jar:
            if not cookie.is_expired() and (cookie.domain == "chatgpt.com" or cookie.domain.endswith(".chatgpt.com")):
                item = {key: getattr(cookie, key) for key in fields}
                item["rest"] = cookie._rest
                cookies.append(item)
        state = self._read()
        state["cookies"] = cookies
        _atomic_json(self.path, state)

    @asynccontextmanager
    async def account_scope(self, session):
        async with self._gate:
            acquired = False
            loaded = False
            try:
                while not acquired:
                    acquired = _try_lock(self._lock_stream)
                    if not acquired:
                        await asyncio.sleep(0.1)
                self.check_backoff()
                self.load_cookies(session)
                loaded = True
                interval = float(os.environ.get("GPT2AGENT_MIN_ACCOUNT_INTERVAL_SECONDS", "5"))
                if interval < 0:
                    raise ValueError("GPT2AGENT_MIN_ACCOUNT_INTERVAL_SECONDS must be >= 0")
                state = self._read()
                wait = float(state.get("last_started_at", 0)) + interval - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.check_backoff()
                state["last_started_at"] = time.time()
                _atomic_json(self.path, state)
                yield
            finally:
                if acquired:
                    try:
                        if loaded:
                            self.save_cookies(session)
                    finally:
                        _unlock(self._lock_stream)

    @asynccontextmanager
    async def async_session(self, backend_session):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("account runtime requires one long-lived asyncio event loop")
        self._loop = loop
        if self._async_session is None:
            self._async_session = AsyncSession(impersonate=IMPERSONATE, verify=True)
        session = self._async_session
        session.cookies.clear()
        session.cookies.update(backend_session.cookies)
        responses = []

        class Transport:
            cookies = session.cookies

            async def get(_, *args, **kwargs):
                self.check_backoff()
                response = await session.get(*args, **kwargs)
                self.note_response(response)
                return response

            async def post(_, *args, **kwargs):
                self.check_backoff()
                response = await session.post(*args, **kwargs)
                self.note_response(response)
                if kwargs.get("stream"):
                    responses.append(response)
                return response

        try:
            self.check_backoff()
            yield Transport()
        finally:
            backend_session.cookies.clear()
            backend_session.cookies.update(session.cookies)
            for response in responses:
                if response.quit_now is not None:
                    response.quit_now.set()
                try:
                    await asyncio.wait_for(response.aclose(), timeout=2)
                except asyncio.TimeoutError:
                    await session.close()
                    self._async_session = None
                    break
                except asyncio.CancelledError:
                    await session.close()
                    self._async_session = None
                    raise

    async def warmup(self, backend_session) -> None:
        """One ordinary homepage GET per worker; use observed deployment metadata."""
        if self._warmed:
            return
        async with self.async_session(backend_session) as session:
            headers = {key: value for key, value in backend_session.headers.items()
                       if key.lower() != "authorization"}
            headers["Accept"] = "text/html"
            response = await session.get("https://chatgpt.com/", headers=headers, timeout=25)
            if response.status_code != 200:
                raise RuntimeError(f"homepage warmup HTTP {response.status_code}; no submit")
            match = re.search(r'data-build="([^"]+)"', response.text)
            if not match:
                if any(marker in response.text.lower() for marker in ("cf-chl", "just a moment", "challenge-platform")):
                    self.backoff()
                    raise RuntimeError("homepage challenge required; no submit")
                raise RuntimeError("homepage deployment metadata unavailable; no submit")
            self.client_version = match.group(1)
            state = self._read()
            state["client_version"] = self.client_version
            state["client_version_observed_at"] = time.time()
            _atomic_json(self.path, state)
            backend_session.headers["OAI-Client-Version"] = self.client_version
            self._warmed = True

    async def aclose(self) -> None:
        if self._async_session is not None:
            await self._async_session.close()
            self._async_session = None
        self._lock_stream.close()
