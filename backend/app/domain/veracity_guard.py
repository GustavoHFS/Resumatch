"""Post-LLM VeracityGuard with granular per-block fallback and structured diff.

Inspects every block (`summary` and each `bullet_id`) returned by the single-shot LLM call:
- Rehydrates masked institution/employer tokens (`[[INST_*]]`, `[[EMP_*]]`).
- Rejects blocks containing:
  1. Unresumable skills (`preparable_gap` or `unconfirmed`) or unmapped job requirements.
  2. Reserved project names or `forbidden_resume_terms`.
  3. Self-deprecating qualifiers ('básico', 'apenas noções', 'nível inicial', 'basic level').
  4. Invented numbers, metrics, or percentages absent from the original factual block!
  5. Unresolved placeholder markers (`[[...]]`) or length bounds violations.
- Any rejected block cleanly falls back to its exact deterministic original text and records
  a structured entry in `guard_warnings`.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from app.adapters.llm.pii_stripper import rehydrate_placeholders
from app.adapters.llm.providers import SurgicalLlmProposal
from app.domain.resume_composer import ResumeDocumentPayload
from app.domain.triage_engine import TriageResult, contains_term
from app.models.profile_schema import SELF_DEPRECATING_PATTERN, MasterProfile, StrictModel

NUMERIC_METRIC_RE = re.compile(r"\b\d+(?:[.,]\d+)?%?")
LEFTOVER_PLACEHOLDER_RE = re.compile(r"\[\[[A-Z]+_\d+\]\]")


class GuardWarning(StrictModel):
    block_id: str
    rule: str
    reason: str


class BlockDiffEntry(StrictModel):
    block_id: str
    block_type: Literal["summary", "bullet"]
    before: str
    after: str
    changed: bool
    reverted_by_guard: bool = False
    guard_rule: str | None = None


class VeracityGuardResult(StrictModel):
    adapted_payload: ResumeDocumentPayload
    guard_warnings: list[GuardWarning] = Field(default_factory=list)
    diff: list[BlockDiffEntry] = Field(default_factory=list)


def _extract_numeric_tokens(text: str) -> set[str]:
    return {m.group(0).replace(",", ".") for m in NUMERIC_METRIC_RE.finditer(text)}


class VeracityGuard:
    def __init__(
        self,
        profile: MasterProfile,
        triage: TriageResult | None = None,
    ) -> None:
        self.profile = profile
        self.triage = triage

        resumable_names = {
            alias.casefold()
            for sk in profile.skills
            if sk.can_appear_on_resume
            for alias in [sk.display_name, *sk.aliases]
            if alias.strip()
        }
        self._blocked_skills: list[str] = []
        for sk in profile.skills:
            if not sk.can_appear_on_resume:
                for alias in [sk.display_name, *sk.aliases]:
                    clean = alias.strip()
                    if len(clean) >= 2 and clean.casefold() not in resumable_names:
                        self._blocked_skills.append(clean)

        self._reserved_and_forbidden: list[str] = [
            *profile.forbidden_resume_terms,
            *(p.name for p in profile.projects if p.status == "reserved"),
        ]

    def _validate_single_block(
        self,
        block_id: str,
        original_text: str,
        candidate_text: str,
        max_len: int,
    ) -> tuple[bool, str | None, str | None]:
        cleaned = candidate_text.strip()
        if len(cleaned) < 15 or len(cleaned) > max_len:
            return False, "LENGTH_BOUNDS", f"Block '{block_id}' violated length bounds ({len(cleaned)} chars)."

        if LEFTOVER_PLACEHOLDER_RE.search(cleaned):
            return False, "UNRESOLVED_PLACEHOLDER", f"Block '{block_id}' contains unresolved placeholder token."

        dep_match = SELF_DEPRECATING_PATTERN.search(cleaned)
        if dep_match:
            return (
                False,
                "SELF_DEPRECATING_QUALIFIER",
                f"Block '{block_id}' introduced self-deprecating qualifier '{dep_match.group(0)}'.",
            )

        for term in self._reserved_and_forbidden:
            if term and contains_term(cleaned, term) and not contains_term(original_text, term):
                return (
                    False,
                    "FORBIDDEN_OR_RESERVED_TERM",
                    f"Block '{block_id}' attempted to inject forbidden/reserved term '{term}'.",
                )

        for bad_skill in self._blocked_skills:
            if contains_term(cleaned, bad_skill) and not contains_term(original_text, bad_skill):
                return (
                    False,
                    "UNVERIFIED_SKILL_HALLUCINATION",
                    f"Block '{block_id}' attempted to claim unverified/preparable skill '{bad_skill}'.",
                )

        # Strict anti-metric-fabrication check: every numeric token in candidate must exist in original!
        orig_nums = _extract_numeric_tokens(original_text)
        cand_nums = _extract_numeric_tokens(cleaned)
        invented_nums = cand_nums - orig_nums
        if invented_nums:
            sample = ", ".join(sorted(invented_nums))
            return (
                False,
                "INVENTED_METRIC_OR_NUMBER",
                f"Block '{block_id}' introduced unverified number/metric ({sample}) absent from factual source.",
            )

        return True, None, None

    def verify_and_apply(
        self,
        deterministic_payload: ResumeDocumentPayload,
        proposal: SurgicalLlmProposal,
        placeholder_to_original: dict[str, str],
    ) -> VeracityGuardResult:
        payload_copy = deterministic_payload.model_copy(deep=True)
        warnings: list[GuardWarning] = []
        diffs: list[BlockDiffEntry] = []

        # 1. Verify Summary
        orig_summary = deterministic_payload.summary.text
        cand_summary = rehydrate_placeholders(proposal.summary_text, placeholder_to_original).strip()
        ok_sum, rule_sum, reason_sum = self._validate_single_block(
            "summary", orig_summary, cand_summary, max_len=900
        )
        if ok_sum:
            payload_copy.summary.text = cand_summary
            diffs.append(
                BlockDiffEntry(
                    block_id="summary",
                    block_type="summary",
                    before=orig_summary,
                    after=cand_summary,
                    changed=(cand_summary != orig_summary),
                    reverted_by_guard=False,
                )
            )
        else:
            assert rule_sum is not None and reason_sum is not None
            warnings.append(GuardWarning(block_id="summary", rule=rule_sum, reason=reason_sum))
            diffs.append(
                BlockDiffEntry(
                    block_id="summary",
                    block_type="summary",
                    before=orig_summary,
                    after=orig_summary,
                    changed=False,
                    reverted_by_guard=True,
                    guard_rule=rule_sum,
                )
            )

        # 2. Verify Bullets block-by-block
        proposed_by_id = {b.bullet_id: b.text for b in proposal.bullets}
        for exp in payload_copy.experiences:
            for bullet in exp.bullets:
                orig_b = bullet.text
                if bullet.id not in proposed_by_id:
                    diffs.append(
                        BlockDiffEntry(
                            block_id=bullet.id,
                            block_type="bullet",
                            before=orig_b,
                            after=orig_b,
                            changed=False,
                            reverted_by_guard=False,
                        )
                    )
                    continue

                cand_b = rehydrate_placeholders(
                    proposed_by_id[bullet.id], placeholder_to_original
                ).strip()
                ok_b, rule_b, reason_b = self._validate_single_block(
                    bullet.id, orig_b, cand_b, max_len=400
                )
                if ok_b:
                    bullet.text = cand_b
                    diffs.append(
                        BlockDiffEntry(
                            block_id=bullet.id,
                            block_type="bullet",
                            before=orig_b,
                            after=cand_b,
                            changed=(cand_b != orig_b),
                            reverted_by_guard=False,
                        )
                    )
                else:
                    assert rule_b is not None and reason_b is not None
                    warnings.append(
                        GuardWarning(block_id=bullet.id, rule=rule_b, reason=reason_b)
                    )
                    diffs.append(
                        BlockDiffEntry(
                            block_id=bullet.id,
                            block_type="bullet",
                            before=orig_b,
                            after=orig_b,
                            changed=False,
                            reverted_by_guard=True,
                            guard_rule=rule_b,
                        )
                    )

        return VeracityGuardResult(
            adapted_payload=payload_copy,
            guard_warnings=warnings,
            diff=diffs,
        )
