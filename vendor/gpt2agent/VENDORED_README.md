# Vendored gpt2agent transport

The `gpt2agent` package under `vendor/gpt2agent/` is the **accepted patched
transport** used by `src/chatgpt_provider/gpt2agent_provider.py`.

## Provenance

- Upstream source: <https://github.com/robotlearning123/gpt2agent.git>
- Upstream commit: `04e3d93`
- Package version: `0.0.11`
- License: MIT (see `vendor/gpt2agent/LICENSE`)
- Origin of this copy: the accepted experiment venv at
  `experiments/gpt2agent/.venv/Lib/site-packages/gpt2agent/` from the
  pre-consolidation worktree (byte-identical to this copy).

## What differs from upstream PyPI / GitHub source

The accepted transport copy is a **patched** build of `0.0.11` with local
fixes landed in the pre-consolidation project. The patched files differ from
the pristine upstream source in:

- `auth.py` — accepted credential handling path
- `install.py` — accepted install/credential-bootstrapping path
- `_log_redact.py` — accepted redaction fixes
- `__init__.py` — package metadata
- `__main__.py` — accepted entrypoint

The original snapshot's transport-critical modules were byte-identical to
upstream `0.0.11`. The 2026-09-26 session patch changes `backend.py`,
`sentinel.py`, `sse.py` and `_vendored/pow.py`, and adds `runtime.py` and
`frontend.py`. It selectively references upstream `e911a3a` (0.0.23), without
replacing the accepted safechain protocol/artifact layer or importing the
upstream simulation/bridge/geo/automatic-retry modules. See
`WEB_SESSION_AUDIT.md` at the repository root for the exact scope and limitations.

## Runtime wiring

`src/chatgpt_provider/gpt2agent_provider.py` imports the package by inserting
this directory into `sys.path` when `provider.gpt2agent.site_packages` points
here (default in `pieces/*.yaml` is the repo-local `vendor/gpt2agent`).

No Playwright is used anywhere in this transport.
