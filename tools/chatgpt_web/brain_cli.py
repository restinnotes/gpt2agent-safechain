"""brain — disposable CLI wrapper over the gpt2agent transport (probe only).

Commands:
  brain start       Start a new persistent Brain chat (mode normal|project).
  brain continue    Continue an existing Brain chat (states dir keyed by --id).
  brain temporary   One-shot fresh temporary chat (isolated).
  brain inspect     Show durable state for --id.

Machine-readable JSON on stdout. State dir default: tools/chatgpt_web/brain_states.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain_lib as bl
import probe_lib as pl

STATE_DIR = bl.PROBE_DIR / "brain_states"


def _state_path(bid: str) -> Path:
    return STATE_DIR / f"{bid}.json"


def _read_state(bid: str) -> dict:
    p = _state_path(bid)
    if not p.exists():
        raise FileNotFoundError(f"brain id not found: {bid}")
    return json.loads(p.read_text(encoding="utf-8"))


async def cmd_start(args) -> dict:
    mode = args.mode
    project = args.project
    prompt = args.prompt
    pid = bl.project_id(project) if project else None
    state = await bl.create_chat(args.id, prompt, mode=mode, project=project, temporary=False)
    if state.get("code") != 200:
        raise RuntimeError(state.get("error") or "create failed")
    entry = {
        "brain_id": args.id,
        "mode": mode,
        "project": project,
        "project_id": pid,
        "conversation_id": state["conversation_id"],
        "parent_message_id": state["message_id"],
        "artifact": None,
    }
    _state_path(args.id).write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


async def cmd_continue(args) -> dict:
    entry = _read_state(args.id)
    prompt = Path(args.prompt).read_text(encoding="utf-8") if args.prompt_file else args.prompt
    attachments = []
    if args.attach:
        backend = bl.make_client()
        for f in args.attach:
            attachments.append(await bl.upload_attachment(
                backend, Path(f), temporary=False, project_id=entry.get("project_id")
            ))
    res = await bl.continue_chat(
        args.id, prompt, attachments=attachments, timeout=args.timeout,
        state_override={"conversation_id": entry["conversation_id"], "message_id": entry["parent_message_id"],
                        "project_id": entry.get("project_id")},
    )
    if res.get("code") != 200:
        raise RuntimeError(res.get("error") or "continue failed")
    artifact = None
    if entry.get("conversation_id"):
        backend = bl.make_client()
        artifact = await bl.download_sandbox_artifact(
            backend, entry["conversation_id"], res.get("text_head") or "",
            res.get("message_id") or entry.get("parent_message_id"),
        )
    # persist updated parent
    entry["parent_message_id"] = res.get("message_id") or entry["parent_message_id"]
    entry["last_text_head"] = (res.get("text_head") or "")[:2000]
    if artifact:
        out_path = Path(artifact["name"])
        out_path.write_bytes(artifact["bytes"])
        entry["artifact"] = {"name": artifact["name"], "bytes": len(artifact["bytes"]),
                             "sandbox_path": artifact["sandbox_path"]}
    _state_path(args.id).write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    out = {k: entry[k] for k in ("brain_id", "mode", "project_id", "conversation_id", "parent_message_id")}
    out["artifact"] = entry.get("artifact")
    out["reply_head"] = (res.get("text_head") or "")[:500]
    return out


async def cmd_temporary(args) -> dict:
    backend = bl.make_client()
    res = await pl.send_turn(backend, args.prompt, temporary=True, timeout=args.timeout)
    return {
        "brain_id": args.id,
        "mode": "temporary",
        "conversation_id": res.get("conv_id"),
        "parent_message_id": res.get("message_id"),
        "reply_head": (res.get("text_head") or "")[:500],
        "code": res.get("code"),
        "error": res.get("error"),
    }


async def cmd_inspect(args) -> dict:
    entry = _read_state(args.id)
    if args.temporary:
        entry = {"brain_id": args.id, "mode": "temporary"}
    return entry


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start")
    p.add_argument("--id", required=True)
    p.add_argument("--mode", choices=["normal", "project"], default="normal")
    p.add_argument("--project", default=None)
    p.add_argument("--prompt", required=True)

    p = sub.add_parser("continue")
    p.add_argument("--id", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--prompt-file")
    p.add_argument("--attach", action="append")
    p.add_argument("--timeout", type=float, default=300)

    p = sub.add_parser("temporary")
    p.add_argument("--id", default="tmp")
    p.add_argument("--prompt", required=True)
    p.add_argument("--timeout", type=float, default=120)

    p = sub.add_parser("inspect")
    p.add_argument("--id", required=True)
    p.add_argument("--temporary", action="store_true")

    args = ap.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if args.command == "start":
            out = asyncio.run(cmd_start(args))
        elif args.command == "continue":
            out = asyncio.run(cmd_continue(args))
        elif args.command == "temporary":
            out = asyncio.run(cmd_temporary(args))
        else:
            out = asyncio.run(cmd_inspect(args))
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()