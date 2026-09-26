"""Opt-in ChatGPT Web batch adapter. One provider, one event loop, no retries.

Uses runner.py's job shape and prompts/outputs/meta directory convention.
Existing runner model routes are unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

DEFAULT_REPO = Path(__file__).resolve().parents[2]


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_jobs(run_dir, jobs, *, repo=DEFAULT_REPO):
    repo = Path(repo).resolve()
    sys.path.insert(0, str(repo / "vendor"))
    sys.path.insert(0, str(repo / "src"))
    from chatgpt_provider.gpt2agent_provider import GPT2AgentProvider
    from chatgpt_provider.protocol import JobConfig, ProviderError, ProviderPhase

    run_dir = Path(run_dir).resolve()
    provider = GPT2AgentProvider(site_packages=str(repo / "vendor"))
    records = []
    try:
        for job in jobs:
            job_id = str(job["job_id"])
            if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
                raise ValueError("job_id must use only letters, digits, underscores or hyphens")
            marker = run_dir / "meta" / f"{job_id}.submitted.json"
            meta_path = run_dir / "meta" / f"{job_id}.json"
            if marker.exists():
                raise RuntimeError(f"{job_id} already reached the submit boundary; inspect its saved result before any resubmit")
            prompt = job["prompt"]
            model = job["model"]  # Explicit server model slug; no automatic model substitution.
            prompt_path = run_dir / "prompts" / f"{job_id}.txt"
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(prompt, encoding="utf-8")
            record = {key: value for key, value in job.items() if key != "prompt"}
            record.update(status="queued", model_key="chatgpt_web", model_name=model,
                          prompt_file=f"prompts/{job_id}.txt", output_file=f"outputs/{job_id}.md",
                          attempts=0, output_chars=0, tool_counts={})
            started = time.monotonic()

            async def before_submit():
                marker.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation also protects duplicate job ids in two workers.
                with marker.open("x", encoding="utf-8") as stream:
                    json.dump({"job_id": job_id, "model": model,
                               "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                record.update(status="submitting", attempts=1)
                _save(meta_path, record)

            _save(meta_path, record)
            try:
                result = await provider.complete_chat(
                    prompt=prompt,
                    config=JobConfig(model=model, thinking_effort=job.get("thinking_effort")),
                    attachments=job.get("attachments"), expect_artifact=job.get("expect_artifact", False),
                    pre_submit=before_submit,
                )
                output = run_dir / record["output_file"]
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(result.text, encoding="utf-8")
                record.update(status="ok", output_chars=len(result.text), provider_meta=result.meta)
                if result.has_artifact:
                    name = Path((result.artifact_name or "artifact.bin").replace("\\", "/")).name
                    artifact = run_dir / "artifacts" / job_id / name
                    artifact.parent.mkdir(parents=True, exist_ok=True)
                    artifact.write_bytes(result.artifact_bytes)
                    record["artifact_file"] = str(artifact.relative_to(run_dir))
            except ProviderError as exc:
                record.update(status="failed", error=str(exc), phase=exc.phase.value,
                              retryable=exc.retryable, ambiguous=exc.ambiguous)
            except Exception as exc:
                record.update(status="failed", error=str(exc), retryable=False,
                              ambiguous=marker.exists(), phase=(ProviderPhase.POST_SUBMIT_AMBIGUOUS.value
                              if marker.exists() else ProviderPhase.PRE_SUBMIT.value))
            finally:
                record["seconds"] = round(time.monotonic() - started, 3)
                _save(meta_path, record)
            records.append(record)
            if record["status"] != "ok":
                break  # Leave the remaining batch queued; never launch retries or fallbacks.
    finally:
        await provider.aclose()
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, required=True, help="UTF-8 JSON array of jobs")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    args = parser.parse_args()
    jobs = json.loads(args.jobs.read_text(encoding="utf-8"))
    records = asyncio.run(run_jobs(args.run_dir, jobs, repo=args.repo))
    print(json.dumps(records, ensure_ascii=False))
    return 0 if len(records) == len(jobs) and all(r["status"] == "ok" for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
