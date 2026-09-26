"""Section 7: Project persistent B -> fresh temp reviewer -> back to B.

Repo-local copy. Uses the vendored gpt2agent transport.
1. Give B a small coding objective (it must remember it).
2. Ask B to produce a frozen candidate with a specific injected bug.
3. Fresh TEMP chat R reviews the objective+candidate (no project access).
4. Return to B with the review verdict request; B must still recall the objective and answer ACCEPT/REJECT/MORE_EVIDENCE.
"""
from __future__ import annotations

import asyncio
import json
import random
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain_lib as bl
import probe_lib as pl

OBJ = "Write a Python function sum_positive(nums) that returns the sum of only positive integers in a list."
OBJECTIVE_PIN = "OBJ_PIN_7129"
BUG_MARK = "BUG_4141"


def rnd(p: str) -> str:
    return p + "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))


async def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else "r1"
    backend = bl.make_client()
    src = bl.load_state("restart_project_test_r2")  # Brain B (project test)
    b_sentinel = src.get("sentinel")

    # B round 1: objective
    r1 = await bl.continue_chat(
        "restart_project_test_r2",
        f"Objective: {OBJECTIVE_PIN} {OBJ} Keep this objective for the rest of the conversation. Reply OK.",
        timeout=240,
    )
    # B round 2: frozen candidate with injected bug (step bug: uses < 0 instead of <= 0)
    candidate = (
        "```python\n"
        "def sum_positive(nums):\n"
        "    total = 0\n"
        "    for n in nums:\n"
        "        if n < 0:   # BUG_MARK placeholder: 0 should be excluded\n"
        "            total += n\n"  # wrong sign
        "    return total\n"
        "```\n"
        f"MARKER={BUG_MARK}"
    )
    r2 = await bl.continue_chat("restart_project_test_r2", f"Produce your frozen candidate now:\n{candidate}\nThis is final. Reply READY.", timeout=240)

    # Fresh temp reviewer R (no project context)
    review_prompt = (
        f"Objective: {OBJECTIVE_PIN} {OBJ}\n\n"
        "Candidate:\n\n"
        f"{candidate}\n\n"
        "Write a short review: does the candidate satisfy the objective? "
        "Report only VERDICT: ACCEPT / REJECT / MORE_EVIDENCE at the end."
    )
    r = await pl.send_turn(backend, review_prompt, temporary=True, timeout=300)

    # Return to B with the review report
    review_text = (r.get("text_head") or "")[:2000]
    r3 = await bl.continue_chat(
        "restart_project_test_r2",
        f"A fresh reviewer returned this report on your candidate:\n\n{review_text}\n\n"
        f"Remember your original objective ({OBJECTIVE_PIN}). Decide: ACCEPT_REVIEW, REJECT_REVIEW, or MORE_EVIDENCE. Reply with only that word.",
        timeout=240,
    )
    out = {
        "PROJECT_B_R_B_LOOP": {
            "tag": tag,
            "project": src["project"],
            "conversation_id": src["conversation_id"],
            "b_recalled_objective_pin": OBJECTIVE_PIN in (r3.get("text_head") or ""),
            "reviewer_verdict_in_text": review_text[:120],
            "b_decision": (r3.get("text_head") or "")[:120],
            "b_original_sentinel_should_still_be_remembered": b_sentinel,
        }
    }
    out["PROJECT_B_R_B_LOOP"]["verdict"] = (
        "PASS" if out["PROJECT_B_R_B_LOOP"]["b_recalled_objective_pin"]
        else "FAIL"
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    pl.save_json("results_reviewer_loop.json", out)


if __name__ == "__main__":
    asyncio.run(main())