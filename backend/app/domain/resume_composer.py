"""Deterministic Resume Composer (< 20ms, Zero AI).

Assembles an immutable `ResumeDocumentPayload` from `MasterProfile` + `AreaPreset`
and an optional `TriageResult`, preserving `factual_source` provenance per item,
applying audited contextual equivalences, supporting dynamic section ordering and
`auto_fit` budget trimming, and enforcing strict veracity blocks.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from app.domain.triage_engine import TriageResult, contains_term
from app.models.profile_schema import (
    MasterProfile,
    ProfileLink,
    SectionKind,
    SectionLabels,
    StrictModel,
    SupportedLocale,
)


class AppliedEquivalenceRecord(StrictModel):
    from_term: str
    to_term: str
    factual_basis: str


class RenderedHeader(StrictModel):
    full_name: str
    location_line: str
    contact_line: str
    email: str
    phone: str
    links: list[ProfileLink]


class RenderedSummary(StrictModel):
    text: str
    factual_source: str


class RenderedEducationItem(StrictModel):
    id: str
    degree_line: str
    institution_line: str
    period_line: str
    status: Literal["completed", "in_progress"]
    factual_source: str


class RenderedBulletItem(StrictModel):
    id: str
    text: str
    score: float = 0.0
    factual_source: str


class RenderedExperienceItem(StrictModel):
    id: str
    title_company_bold: str
    meta_suffix: str
    bullets: list[RenderedBulletItem]
    factual_source: str


class RenderedCourseItem(StrictModel):
    id: str
    entry_type: Literal["course", "practical_assessment"]
    bold_prefix: str
    rest_text: str
    factual_source: str


class RenderedProjectItem(StrictModel):
    id: str
    name: str
    summary: str | None = None
    url: str | None = None
    bullets: list[RenderedBulletItem] = Field(default_factory=list)
    factual_source: str


class RenderedSkillGroup(StrictModel):
    id: str
    label: str
    items_text: str
    score: float = 0.0
    factual_source: str


class RenderedLanguageItem(StrictModel):
    id: str
    language: str
    proficiency: str


class ResumeDocumentPayload(StrictModel):
    area_id: str
    display_name: str
    locale: SupportedLocale
    max_pages: int
    compact_mode: bool = False
    output_folder: str
    output_pdf_filename: str
    section_labels: SectionLabels
    section_order: list[SectionKind]
    page_break_before_sections: list[SectionKind] = Field(default_factory=list)

    header: RenderedHeader
    summary: RenderedSummary
    education: list[RenderedEducationItem]
    experiences: list[RenderedExperienceItem]
    courses: list[RenderedCourseItem] = Field(default_factory=list)
    projects: list[RenderedProjectItem] = Field(default_factory=list)
    skill_groups: list[RenderedSkillGroup]
    languages: list[RenderedLanguageItem]

    applied_equivalences: list[AppliedEquivalenceRecord] = Field(default_factory=list)
    auto_fit_adjustments: list[str] = Field(default_factory=list)


def _join_natural(items: list[str], locale: SupportedLocale) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    conj = " and " if locale == "en-US" else " e "
    if locale == "en-US" and len(items) > 2:
        return ", ".join(items[:-1]) + f",{conj}" + items[-1]
    return ", ".join(items[:-1]) + conj + items[-1]


class DeterministicResumeComposer:
    """Stateless deterministic resume builder (< 20ms, 0 LLM tokens)."""

    def __init__(self, profile: MasterProfile) -> None:
        self.profile = profile
        self.skills_by_id = {s.id: s for s in profile.skills}
        self.courses_by_id = {c.id: c for c in profile.courses}
        self.projects_by_id = {p.id: p for p in profile.projects}

    def _apply_equivalences(
        self,
        text: str,
        job_description: str,
        applied_log: dict[tuple[str, str], AppliedEquivalenceRecord],
    ) -> str:
        if not job_description:
            return text
        updated = text
        for eq in self.profile.contextual_equivalences:
            if any(contains_term(job_description, trig) for trig in eq.trigger_terms):
                if eq.from_term in updated and eq.to_term not in updated:
                    pattern = re.compile(r"(?<!\w)" + re.escape(eq.from_term) + r"(?!\w)")
                    new_val = pattern.sub(eq.to_term, updated)
                    if new_val != updated:
                        updated = new_val
                        applied_log[(eq.from_term, eq.to_term)] = AppliedEquivalenceRecord(
                            from_term=eq.from_term,
                            to_term=eq.to_term,
                            factual_basis=eq.factual_basis,
                        )
        return updated

    def _score_text_against_triage(
        self,
        text: str,
        skill_ids: list[str],
        triage: TriageResult | None,
    ) -> float:
        if triage is None:
            return 0.0
        eligible_reqs = [r for r in triage.requirements if r.can_appear_on_resume]
        if not eligible_reqs:
            return 0.0

        score = 0.0
        weights = {"mandatory": 3.0, "responsibility": 2.0, "desirable": 1.0, "optional": 0.5, "unspecified": 0.5}
        for req in eligible_reqs:
            weight = weights.get(req.requirement_kind, 1.0)
            if req.skill_id in skill_ids:
                score += weight
                continue
            sk = self.skills_by_id.get(req.skill_id)
            aliases = [sk.display_name, *sk.aliases] if sk else req.matched_keywords
            if any(contains_term(text, alias) for alias in aliases if alias):
                score += weight

        if contains_term(text, "SQL") and not any(r.skill_id == "sql" for r in eligible_reqs):
            score -= 0.15
        return round(score, 3)

    def compose(
        self,
        area_id: str,
        *,
        triage: TriageResult | None = None,
        job_description: str = "",
        section_order_override: list[SectionKind] | None = None,
        max_bullets_override: int | None = None,
        max_skill_groups_override: int | None = None,
        compact_mode: bool = False,
        auto_fit_notes: list[str] | None = None,
    ) -> ResumeDocumentPayload:
        if area_id not in self.profile.area_presets:
            raise ValueError(f"Unknown area_id '{area_id}' in MasterProfile.area_presets")

        preset = self.profile.area_presets[area_id]
        locale = preset.locale
        applied_eqs: dict[tuple[str, str], AppliedEquivalenceRecord] = {}

        # 1. Header Assembly
        hdr = self.profile.header
        loc_segments: list[str] = []
        if preset.show_age and hdr.age is not None:
            age_lbl = f"{hdr.age} years old" if locale == "en-US" else f"{hdr.age} anos"
            loc_segments.append(age_lbl)

        place_parts: list[str] = []
        if preset.show_neighborhood and hdr.neighborhood:
            place_parts.append(hdr.neighborhood)
        city_state = f"{hdr.city} – {hdr.state}"
        place_parts.append(city_state)
        place_str = ", ".join(place_parts)
        if locale == "en-US":
            country = hdr.country_by_locale.get("en-US", "Brazil")
            place_str = f"{place_str}, {country}"
        loc_segments.append(place_str)

        rendered_header = RenderedHeader(
            full_name=hdr.full_name,
            location_line=" • ".join(loc_segments),
            contact_line=f"{hdr.email} • {hdr.phone}",
            email=hdr.email,
            phone=hdr.phone,
            links=list(hdr.links),
        )

        # 2. Summary Assembly (adaptive when triage matches allowed_summary_skill_ids)
        summary_text = preset.default_summary
        summary_source = f"preset:{area_id}.default_summary"
        if triage is not None:
            eligible_req_ids = {r.skill_id for r in triage.requirements if r.can_appear_on_resume}
            ranked_summary_skills: list[tuple[float, int, str, str]] = []
            for pos, sk_id in enumerate(preset.allowed_summary_skill_ids):
                if sk_id not in eligible_req_ids:
                    continue
                sk = self.skills_by_id.get(sk_id)
                if not sk or not sk.can_appear_on_resume:
                    continue
                lbl = sk.translations.get(locale) or sk.summary_label or sk.display_name
                sc = self._score_text_against_triage(
                    " ".join([sk.display_name, *sk.aliases]), [sk.id], triage
                )
                ranked_summary_skills.append((sc, pos, sk.veracity, lbl))

            if ranked_summary_skills:
                ranked_summary_skills.sort(key=lambda item: (-item[0], item[1]))
                top_three = ranked_summary_skills[:3]
                labels = [item[3] for item in top_three]
                joined = _join_natural(labels, locale)
                if locale == "en-US":
                    summary_text = f"{preset.summary_prefix} Skilled in {joined}."
                else:
                    all_practice = all(item[2] == "confirmed_practice" for item in top_three)
                    connector = (
                        " Atuação com "
                        if area_id == "suporte" and all_practice
                        else " Conhecimentos em "
                    )
                    summary_text = f"{preset.summary_prefix}{connector}{joined}."
                    if area_id in {"suporte", "cybersec", "administrativo"} and "UNICAP" in preset.default_summary and "UNICAP" not in summary_text:
                        in_prog = next(
                            (e for e in self.profile.education if e.status == "in_progress"),
                            None,
                        )
                        if in_prog:
                            summary_text += f" Graduando em {in_prog.degree.replace('Graduação em ', '')} na {in_prog.institution.split('—')[0].strip()}."
                summary_source = "Adaptive deterministic summary from matched confirmed/academic skills"

        summary_text = self._apply_equivalences(summary_text, job_description, applied_eqs)

        # 3. Education Assembly
        rendered_edu: list[RenderedEducationItem] = []
        for edu in self.profile.education:
            if edu.areas and area_id not in edu.areas:
                continue
            tr = edu.translations.get(locale)
            deg = tr.degree if tr else edu.degree
            inst = tr.institution if tr else edu.institution
            note = tr.program_note if tr else edu.program_note
            period = tr.period_label if tr else edu.period_label

            if edu.status == "in_progress":
                suffix = " – In progress" if locale == "en-US" else " – Em andamento"
                deg_line = deg if ("progress" in deg.lower() or "andamento" in deg.lower()) else f"{deg}{suffix}"
            else:
                deg_line = deg

            inst_line = f"{inst} ({note})" if note and note not in inst else inst
            rendered_edu.append(
                RenderedEducationItem(
                    id=edu.id,
                    degree_line=deg_line,
                    institution_line=inst_line,
                    period_line=period,
                    status=edu.status,
                    factual_source=edu.factual_source,
                )
            )

        # 4. Experience & Deterministic Bullet Selection
        bullet_limit = max_bullets_override or preset.max_bullets_per_experience
        preferred_order = {bid: idx for idx, bid in enumerate(preset.preferred_bullet_ids)}
        rendered_exps: list[RenderedExperienceItem] = []

        for exp in self.profile.experiences:
            exp_tr = exp.translations.get(locale)
            role = exp_tr.role if exp_tr else exp.role
            emp_type = exp_tr.employment_type_label if exp_tr else exp.employment_type_label
            period = exp_tr.period_label if exp_tr else exp.period_label

            meta_parts = [p for p in [emp_type, period] if p]
            meta_suffix = (" | " + " | ".join(meta_parts)) if meta_parts else ""

            candidate_bullets = [
                b for b in exp.bullets if (area_id in b.areas or not b.areas)
            ]
            if not candidate_bullets:
                candidate_bullets = list(exp.bullets)

            scored_bullets: list[tuple[float, int, int, RenderedBulletItem]] = []
            for orig_idx, b in enumerate(candidate_bullets):
                raw_txt = b.translations.get(locale) or b.text
                adapted_txt = self._apply_equivalences(raw_txt, job_description, applied_eqs)
                triage_score = self._score_text_against_triage(adapted_txt, b.skill_tags, triage)
                is_pref = 1 if b.id in preferred_order else 0
                pref_rank = preferred_order.get(b.id, 999)
                # When triage is absent, strictly preserve the preset's preferred_bullet_ids first!
                effective_score = triage_score + (0.5 if is_pref else 0.0) if triage else (10.0 if is_pref else 0.0)
                scored_bullets.append(
                    (
                        effective_score,
                        pref_rank,
                        orig_idx,
                        RenderedBulletItem(
                            id=b.id,
                            text=adapted_txt,
                            score=effective_score,
                            factual_source=b.factual_source,
                        ),
                    )
                )

            scored_bullets.sort(key=lambda item: (-item[0], item[1], item[2]))
            chosen_bullets = [item[3] for item in scored_bullets[:bullet_limit]]
            rendered_exps.append(
                RenderedExperienceItem(
                    id=exp.id,
                    title_company_bold=f"{role} — {exp.company}",
                    meta_suffix=meta_suffix,
                    bullets=chosen_bullets,
                    factual_source=exp.factual_source,
                )
            )

        # 5. Courses & Practical Assessments (strictly completed & included in preset)
        rendered_courses: list[RenderedCourseItem] = []
        for c_id in preset.included_course_ids:
            course = self.courses_by_id.get(c_id)
            if not course or course.status != "completed":
                continue
            c_tr = course.translations.get(locale)
            c_title = c_tr.title if c_tr else course.title
            c_inst = c_tr.institution if c_tr else course.institution
            c_det = c_tr.details_label if c_tr else course.details_label
            if course.entry_type == "practical_assessment":
                rendered_courses.append(
                    RenderedCourseItem(
                        id=course.id,
                        entry_type="practical_assessment",
                        bold_prefix=f"{c_title}: ",
                        rest_text=f"{c_det.rstrip('.')}.",
                        factual_source=course.factual_source,
                    )
                )
            else:
                rendered_courses.append(
                    RenderedCourseItem(
                        id=course.id,
                        entry_type="course",
                        bold_prefix=f"{c_title} — {c_inst}",
                        rest_text=f" | {c_det}",
                        factual_source=course.factual_source,
                    )
                )

        # 6. Approved Projects Only (reserved projects strictly blocked)
        rendered_projects: list[RenderedProjectItem] = []
        for p_id in preset.included_project_ids:
            proj = self.projects_by_id.get(p_id)
            if not proj or proj.status != "approved":
                continue
            p_bullets = [
                RenderedBulletItem(
                    id=b.id,
                    text=b.translations.get(locale) or b.text,
                    factual_source=b.factual_source,
                )
                for b in proj.bullets
            ]
            rendered_projects.append(
                RenderedProjectItem(
                    id=proj.id,
                    name=proj.name,
                    summary=proj.summary,
                    url=proj.url,
                    bullets=p_bullets,
                    factual_source=proj.factual_source,
                )
            )

        # 7. Skill Groups Scoring & Ordering
        scored_groups: list[tuple[float, int, RenderedSkillGroup]] = []
        for g_idx, grp in enumerate(preset.skill_groups):
            adapted_items = self._apply_equivalences(grp.items_text, job_description, applied_eqs)
            g_score = self._score_text_against_triage(
                f"{grp.label} {adapted_items}", grp.skill_ids, triage
            )
            scored_groups.append(
                (
                    g_score,
                    g_idx,
                    RenderedSkillGroup(
                        id=grp.id,
                        label=grp.label,
                        items_text=adapted_items,
                        score=g_score,
                        factual_source=grp.factual_source,
                    ),
                )
            )
        if triage is not None:
            scored_groups.sort(key=lambda item: (-item[0], item[1]))
        group_limit = max_skill_groups_override or len(scored_groups)
        chosen_groups = [item[2] for item in scored_groups[:group_limit]]

        # 8. Languages Assembly
        rendered_langs: list[RenderedLanguageItem] = []
        for lang in self.profile.languages:
            l_tr = lang.translations.get(locale)
            rendered_langs.append(
                RenderedLanguageItem(
                    id=lang.id,
                    language=l_tr.language if l_tr else lang.language,
                    proficiency=l_tr.proficiency if l_tr else lang.proficiency,
                )
            )

        # Order English first when locale == 'en-US'
        if locale == "en-US":
            rendered_langs.sort(key=lambda l: 0 if "english" in l.language.lower() else 1)

        effective_order = list(section_order_override or preset.section_order)
        # Drop empty optional sections so we never emit an empty section header
        if not rendered_courses and "courses" in effective_order:
            effective_order.remove("courses")
        if not rendered_projects and "projects" in effective_order:
            effective_order.remove("projects")

        reserved_names = [p.name for p in self.profile.projects if p.status == "reserved"]
        all_forbidden = [*self.profile.forbidden_resume_terms, *reserved_names]

        payload = ResumeDocumentPayload(
            area_id=area_id,
            display_name=preset.display_name,
            locale=locale,
            max_pages=preset.max_pages,
            compact_mode=compact_mode,
            output_folder=preset.output_folder,
            output_pdf_filename=preset.output_pdf_filename,
            section_labels=preset.section_labels,
            section_order=effective_order,
            page_break_before_sections=list(preset.page_break_before_sections),
            header=rendered_header,
            summary=RenderedSummary(text=summary_text, factual_source=summary_source),
            education=rendered_edu,
            experiences=rendered_exps,
            courses=rendered_courses,
            projects=rendered_projects,
            skill_groups=chosen_groups,
            languages=rendered_langs,
            applied_equivalences=list(applied_eqs.values()),
            auto_fit_adjustments=list(auto_fit_notes or []),
        )

        # Final post-composition veracity guard
        self._assert_no_forbidden_leaks(payload, all_forbidden)
        return payload

    def _assert_no_forbidden_leaks(
        self, payload: ResumeDocumentPayload, forbidden_terms: list[str]
    ) -> None:
        corpus_parts = [
            payload.summary.text,
            *(e.degree_line + " " + e.institution_line for e in payload.education),
            *(b.text for exp in payload.experiences for b in exp.bullets),
            *(c.bold_prefix + " " + c.rest_text for c in payload.courses),
            *(p.name + " " + (p.summary or "") for p in payload.projects),
            *(g.label + " " + g.items_text for g in payload.skill_groups),
        ]
        corpus = "\n".join(corpus_parts)
        for forbidden in forbidden_terms:
            if forbidden and contains_term(corpus, forbidden):
                raise ValueError(
                    f"Veracity block: forbidden/reserved term '{forbidden}' detected in composed resume."
                )
