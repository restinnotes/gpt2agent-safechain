# gpt2agent-safechain

2026-09-26 session update: one queued account worker, persistent account IDs and
cookie jar, frontend prepare/conduit flow, and fail-fast protection cooldowns.
See [audit and music-project integration](WEB_SESSION_AUDIT.md). No claim is
made that an unofficial Web client prevents account flags or model fallback.

Agent calls ChatGPT web-quota through a vendored `gpt2agent` transport, with
anti-ban design so automated traffic stays human-scale.

## Goal

- Agent self-drives ChatGPT web (Plus/Pro) quota: `chat` / `agent` /
  `deep_research` / `code_interpreter` / `generate_image` via
  `chatgpt.com/backend-api/*` (Temporary Chat, no server-side history).
- Avoid account flags/rate-limits: human pacing, backoff, challenge handling,
  temporary chats by default.

## Layout

- `vendor/gpt2agent/` — vendored transport (pinned upstream commit, see
  `VENDORED_README.md`). Reads token from `$CODEX_HOME/auth.json`
  (falls back to `~/.codex/auth.json`).
- `src/chatgpt_provider/` — thin adapter: failure classification
  (`PRE_SUBMIT` safe-retry vs `POST_SUBMIT_AMBIGUOUS`), readiness probe,
  sandbox artifact download. No sentinel/auth re-implementation.
- `tools/chatgpt_web/` — brain/smoke/probe scripts.
- `tests/test_gpt2agent_provider.py` — provider tests.
- `pieces/` — provider wiring configs.
- `GPT2AGENT_LIBRECHAT_FEATURE_MAPPING.md` — LibreChat feature mapping.
- `TEXTURE_FORM_GPT2AGENT_PROVENANCE_VERIFICATION.md` — provenance notes.

## Anti-ban rules (enforced by adapter + transport)

1. Temporary chats only (`temporary=True`).
2. Min interval between turns, jitter, no bursts.
3. 429/usage-limit: exponential backoff, fail fast with reset time.
4. Challenge/Sentinel failure: backoff, never brute-force; browser fallback.
5. One browser profile per account; token stays local, sent only to
   `chatgpt.com`.

## Quickstart

```bash
pip install -e ./vendor/gpt2agent  # or pipx install gpt2agent
gpt2agent doctor                    # read-only probe, no quota spent
```

Multi-account: `CODEX_HOME=~/.codex-alt` selects the other login without
touching the default one.
