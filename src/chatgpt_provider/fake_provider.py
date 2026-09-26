"""Deterministic fake ChatGPT provider for offline runs and tests.

No network, no gpt2agent. Behavior is driven by a scenario dict so the whole
state machine (Generator x N -> freeze -> Builder -> Reviewer -> Judge ->
convergence -> final freeze -> Display Editor) can be exercised end-to-end
locally, including every failure path.

Job type is detected from the frozen prompt text (stable markers). Call counts
are tracked per job type so tests can assert duplicate-submit prevention.
"""
from __future__ import annotations

import io
import zipfile

from .protocol import ChatJobResult, JobConfig, PreSubmitHook, ProviderError

_GEN_MARKER = "请查阅可靠资料，然后用人话讲讲"
_BUILDER_MARKER = "# Initial Package Builder"
_REVIEWER_MARKER = "请把你收到的 ZIP 视为一个需要独立重新审计的内容系统"
_REVIEWER_MARKER_V2 = "请把你收到的 ZIP 视为一个需要**独立重新审计**的内容系统"
_DISPLAY_MARKER = "# Display Editor"
_JUDGE_MARKER = "Content Judge Machine Output Contract"
_V2_CONTRACT_MARKER = "CONTENT_PACKAGE_V2_AFFECT_WHY_NOW"


class SimulatedCrash(RuntimeError):
    """Injected crash to test resume semantics (leaves an in-flight submit)."""


class FakeProvider:
    """ChatProvider with scripted deterministic behaviour.

    ``scenario`` maps job type -> mode:

    - generator: ok | fail_pre | crash
    - builder:   ok | missing_file | bad_raw | corrupt_zip | fail_pre | crash
    - reviewer:  ok | identical | markdown_only | missing_file | bad_raw | crash
    - display:   ok | invalid_schema | missing_audit | fail_pre | crash

    ``call_counts[job]`` is incremented on every chat call (before any failure)
    so tests can assert that resume does not re-consume completed jobs.
    """

    def __init__(self, scenario: dict[str, str] | None = None) -> None:
        self.scenario = dict(scenario or {})
        self.call_counts: dict[str, int] = {}
        self._gen_index = 0

    # ------------------------------------------------------------- helpers
    def _mode(self, job: str) -> str:
        return self.scenario.get(job, "ok")

    def _count(self, job: str) -> None:
        self.call_counts[job] = self.call_counts.get(job, 0) + 1

    def _detect_job(self, prompt: str) -> str:
        if _GEN_MARKER in prompt:
            return "generator"
        if _BUILDER_MARKER in prompt:
            return "builder"
        if _REVIEWER_MARKER in prompt or _REVIEWER_MARKER_V2 in prompt:
            return "reviewer"
        if _DISPLAY_MARKER in prompt:
            return "display"
        if _JUDGE_MARKER in prompt:
            return "judge"
        return "chat"

    def _is_v2(self, prompt: str) -> bool:
        return _V2_CONTRACT_MARKER in prompt

    @staticmethod
    def _read_attachment(path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    @staticmethod
    def _rewrite_member(zip_bytes: bytes, member: str, new_bytes: bytes) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zin, zipfile.ZipFile(
            buf, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                zout.writestr(item, new_bytes if item.filename == member else data)
        return buf.getvalue()

    @staticmethod
    def _strip_members(zip_bytes: bytes, members: list[str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zin, zipfile.ZipFile(
            buf, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            for item in zin.infolist():
                if item.filename not in members:
                    zout.writestr(item, zin.read(item.filename))
        return buf.getvalue()

    # ------------------------------------------------------------- protocol
    async def complete_chat(
        self,
        *,
        prompt: str,
        config: JobConfig,
        attachments: list[str] | None = None,
        expect_artifact: bool = False,
        pre_submit: PreSubmitHook | None = None,
    ) -> ChatJobResult:
        if pre_submit is not None:
            await pre_submit()
        job = self._detect_job(prompt)
        self._count(job)
        mode = self._mode(job)

        if mode == "crash":
            raise SimulatedCrash(f"injected crash in {job}")
        if mode == "fail_pre":
            raise ProviderError(f"pre-submit failure in {job}")  # retryable

        if job == "generator":
            return self._generator(prompt)
        v2 = self._is_v2(prompt)
        if job == "builder":
            return self._builder(attachments or [], mode, v2=v2)
        if job == "reviewer":
            return self._reviewer(attachments or [], mode, v2=v2)
        if job == "display":
            return self._display(attachments or [], mode, v2=v2)
        if job == "judge":
            return self._judge(mode)
        return ChatJobResult(text="fake generic reply")

    # ------------------------------------------------------------- jobs
    def _generator(self, prompt: str) -> ChatJobResult:
        self._gen_index += 1
        idx = self._gen_index
        text = (
            f"RAW_FAKE_{idx:04d}\n\n"
            f"这是第 {idx} 个独立 Generator 的确定性输出。\n"
            "作品经历三个阶段：开头的不安逐渐积累，中段被推向强烈的高潮，"
            "最后在疲惫与残留的尊严中结束。\n"
            f"substitution_hint={prompt.count('肖邦')} chars"
        )
        return ChatJobResult(text=text, meta={"fake_index": idx})

    def _builder(self, attachments: list[str], mode: str, *, v2: bool = False) -> ChatJobResult:
        from music_pipeline.content_spec import build_canonical_package, build_canonical_package_v2

        if not attachments:
            return ChatJobResult(text="(no RAW attachment uploaded)")
        raw_bytes = self._read_attachment(attachments[0])
        pkg = build_canonical_package_v2(raw_bytes) if v2 else build_canonical_package(raw_bytes)
        if mode == "ok":
            return ChatJobResult(
                text="Builder produced initial package.",
                artifact_bytes=pkg,
                artifact_name="initial_package.zip",
            )
        if mode == "missing_file":
            stripped = self._strip_members(pkg, ["REVIEW_SUMMARY.md"])
            return ChatJobResult(text="pkg", artifact_bytes=stripped, artifact_name="p.zip")
        if mode == "bad_raw":
            bad = build_canonical_package_v2(raw_bytes + b"\n# tampered\n") if v2 else build_canonical_package(raw_bytes + b"\n# tampered\n")
            return ChatJobResult(text="pkg", artifact_bytes=bad, artifact_name="p.zip")
        if mode == "corrupt_zip":
            return ChatJobResult(text="pkg", artifact_bytes=b"this is not a zip", artifact_name="p.zip")
        if mode == "markdown_only":
            return ChatJobResult(text="Initial package description only.")
        return ChatJobResult(text="pkg", artifact_bytes=pkg, artifact_name="p.zip")

    def _reviewer(self, attachments: list[str], mode: str, *, v2: bool = False) -> ChatJobResult:
        from music_pipeline.content_spec import build_canonical_package, build_canonical_package_v2

        if not attachments:
            return ChatJobResult(text="(no champion ZIP uploaded)")
        champion = self._read_attachment(attachments[0])
        if mode == "identical":
            return ChatJobResult(
                text="Review complete, no changes required.",
                artifact_bytes=champion,
                artifact_name="challenger.zip",
            )
        if mode == "markdown_only":
            return ChatJobResult(text="REVIEW: I found no substantive issues.")
        if mode == "missing_file":
            stripped = self._strip_members(champion, ["02_First_Listen.md"])
            return ChatJobResult(text="review", artifact_bytes=stripped, artifact_name="c.zip")
        if mode == "bad_raw":
            raw = zipfile.ZipFile(io.BytesIO(champion)).read("00_RAW_Master_VERBATIM.md")
            pkg = build_canonical_package_v2(raw + b"\n# reviewer tampered raw\n") if v2 else build_canonical_package(raw + b"\n# reviewer tampered raw\n")
            return ChatJobResult(text="review", artifact_bytes=pkg, artifact_name="c.zip")
        # "ok" / "regression" -> valid challenger, modified Guided Full
        gf = zipfile.ZipFile(io.BytesIO(champion)).read("03_Guided_Full.md").decode("utf-8")
        extra = "REVIEWER_EDIT_SENTINEL 补充一处对关键关系的明确表达。\n"
        if gf.endswith("\n"):
            gf = gf + extra
        else:
            gf = gf + "\n" + extra
        modified = self._rewrite_member(
            champion, "03_Guided_Full.md", gf.encode("utf-8")
        )
        return ChatJobResult(text="review", artifact_bytes=modified, artifact_name="challenger.zip")

    def _judge(self, mode: str) -> ChatJobResult:
        import json

        if mode == "human":
            verdict, recommended, preferred = "INCONCLUSIVE", "INCONCLUSIVE", "INCONCLUSIVE"
            difference, regression, magnitude, issue, value = (
                "INCONCLUSIVE", "INCONCLUSIVE", "INCONCLUSIVE", "INCONCLUSIVE", "HUMAN_REVIEW"
            )
        elif mode == "continue":
            verdict, recommended, preferred = "PACKAGE_B_SLIGHTLY_BETTER", "B", "B"
            difference, regression, magnitude, issue, value = (
                "MINOR_ONLY", "NO", "SMALL_BUT_REAL", "YES", "CONTINUE_REVIEWER"
            )
        else:
            verdict, recommended, preferred = "ROUGHLY_EQUIVALENT", "EQUIVALENT", "EQUIVALENT"
            difference, regression, magnitude, issue, value = (
                "EQUIVALENT", "NO", "ESSENTIALLY_EQUIVALENT", "NO", "CONVERGED"
            )
        result = {
            "schema_version": 2,
            "package_mapping": {"package_a": "SLOT_A.zip", "package_b": "SLOT_B.zip"},
            "pairwise_verdict": verdict, "recommended_package": recommended,
            "preferred_slot": preferred, "difference": difference,
            "comparable_regression": regression,
            "remaining_difference_magnitude": magnitude,
            "remaining_material_issue": issue, "remaining_review_value": value,
            "rationale_short": "deterministic fake Judge verdict",
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("JUDGE_RESULT.json", json.dumps(result, ensure_ascii=False))
            archive.writestr("BLIND_AB_JUDGE_REPORT.md", "# Fake blind report\n")
        return ChatJobResult(text="Judge complete.", artifact_bytes=buf.getvalue(), artifact_name="judge_output.zip")

    @staticmethod
    def _structure_from_frozen_zip(zip_bytes: bytes) -> dict:
        import tempfile

        from music_pipeline.package import validate_package

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            raw = z.read("00_RAW_Master_VERBATIM.md")
        with tempfile.TemporaryDirectory() as td:
            p = f"{td}/frozen.zip"
            with open(p, "wb") as fh:
                fh.write(zip_bytes)
            result = validate_package(p, raw)
            if not result.ok:
                raise ProviderError(
                    "fake display: frozen package invalid: " + "; ".join(result.errors)
                )
            return {
                "editorial_refs": result.structure.editorial_refs,
                "content_contract": result.structure.content_contract,
                "products": {
                    product: [
                        {
                            "section_ref": s.section_ref,
                            "kind": s.kind,
                            "blocks": [
                                {"block_ref": b.block_ref, "editorial_refs": b.editorial_refs,
                                 "text": b.text, "role": b.role, "relation": b.relation,
                                 "kind": b.kind}
                                for b in s.blocks
                            ],
                        }
                        for s in result.structure.product_sections(product)
                    ]
                    for product in ("First Listen", "Guided Full")
                },
            }

    def _display(self, attachments: list[str], mode: str, *, v2: bool = False) -> ChatJobResult:
        from music_pipeline.content_spec import build_display_plan_package, build_display_plan_package_v2

        def _build(structure: dict) -> bytes:
            if v2 or (structure.get("content_contract") == "CONTENT_PACKAGE_V2_AFFECT_WHY_NOW"):
                return build_display_plan_package_v2(structure)
            return build_display_plan_package(structure)

        if mode == "ok":
            if not attachments:
                return ChatJobResult(text="(no frozen package uploaded)")
            structure = self._structure_from_frozen_zip(self._read_attachment(attachments[0]))
            pkg = _build(structure)
            return ChatJobResult(
                text="Display plan generated.",
                artifact_bytes=pkg,
                artifact_name="display_plan.zip",
            )
        if mode == "missing_audit":
            if not attachments:
                return ChatJobResult(text="(no frozen package uploaded)")
            structure = self._structure_from_frozen_zip(self._read_attachment(attachments[0]))
            pkg = _build(structure)
            pkg = self._strip_members(pkg, ["DISPLAY_PLAN_AUDIT.md"])
            return ChatJobResult(text="plan", artifact_bytes=pkg, artifact_name="d.zip")
        # invalid_schema -> plan that fails validation (drops Guided Full)
        if not attachments:
            return ChatJobResult(text="(no frozen package uploaded)")
        structure = self._structure_from_frozen_zip(self._read_attachment(attachments[0]))
        from music_pipeline.content_spec import build_display_plan_package

        valid = _build(structure)
        import json as _json

        with zipfile.ZipFile(io.BytesIO(valid)) as z:
            plan = _json.loads(z.read("DISPLAY_PLAN.json"))
        plan["products"] = plan["products"][:1]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("DISPLAY_PLAN.json", _json.dumps(plan, ensure_ascii=False).encode("utf-8"))
            z.writestr("DISPLAY_PLAN_AUDIT.md", b"audit")
        return ChatJobResult(text="plan", artifact_bytes=buf.getvalue(), artifact_name="d.zip")
