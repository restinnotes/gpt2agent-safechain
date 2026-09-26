#!/usr/bin/env python
"""OpenChamber smoke: brain CLI start -> continue with ZIP attach -> JSON/ZIP -> read.

Repo-local: uses the vendored gpt2agent transport and this repo's python.
"""
import asyncio
import json
import random
import string
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_lib as bl
import probe_lib as pl
from brain_cli import cmd_start, cmd_continue, _read_state  # reuse CLI logic

CLI = Path(__file__).resolve().parent / "brain_cli.py"
PY = sys.executable


def rnd(p):
    return p + "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))


async def main() -> None:
    hidden = rnd("SMK_")
    decision = rnd("OBJ_")
    zpath = bl.PROBE_DIR / "artifacts" / "smoke-probe.zip"
    pl.make_zip(zpath, {"hidden.txt": f"SECRET={hidden}", "task.md": "Produce smoke-result.zip with result.json+notes.md in /mnt/data."})

    # 1) shell -> brain start
    import subprocess
    start = subprocess.run(
        [str(PY), str(CLI), "start", "--id", "smoke-zip", "--mode", "normal",
         "--prompt", f"Remember ARCH_DECISION={decision}. Reply OK."],
        capture_output=True, text=True, timeout=400,
    )
    start_json = json.loads(start.stdout)

    # 2) shell -> brain continue --attach round.zip
    cont = subprocess.run(
        [str(PY), str(CLI), "continue", "--id", "smoke-zip", "--attach", str(zpath),
         "--prompt", "Read the attached zip. Recall ARCH_DECISION from history. Write result.json and notes.md with the secret and decision, package as smoke-result.zip under /mnt/data (NOT /tmp), and give its sandbox path."],
        capture_output=True, text=True, timeout=600,
    )
    cont_json = json.loads(cont.stdout)

    # 3) current agent reads zip artifact
    artifact = None
    if cont_json.get("artifact"):
        apath = Path(cont_json["artifact"]["name"])
        if apath.exists():
            with zipfile.ZipFile(apath) as zf:
                contents = {n: zf.read(n).decode("utf-8", "replace") for n in zf.namelist()}
            joined = str(contents)
            artifact = {
                "has_secret": hidden in joined,
                "has_decision": decision in joined,
                "manifest": contents,
            }
    out = {
        "OPENCHAMBER_DIRECT_CLI": {
            "start_ok": start_json.get("conversation_id") is not None,
            "continue_ok": cont_json.get("conversation_id") is not None,
            "conversation_id": cont_json.get("conversation_id"),
            "parent_message_id": cont_json.get("parent_message_id"),
            "continue_reply_head": (cont_json.get("reply_head") or "")[:200],
            "artifact": artifact,
            "verdict": "PASS" if (artifact and artifact["has_secret"] and artifact["has_decision"]) else "FAIL",
        }
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    pl.save_json("results_smoke.json", out)


if __name__ == "__main__":
    asyncio.run(main())