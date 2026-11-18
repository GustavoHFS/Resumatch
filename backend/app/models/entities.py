"""SQLModel relational entities for Resumatch local persistence (`app.sqlite3`).

Stores structured metadata, deduplication fingerprints, verification evidence,
resume version SHA-256 digests, and application lifecycle events — with ZERO binary BLOBs.
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Field, SQLModel


class JobOpportunity(SQLModel, table=True):
    __tablename__ = "job_opportunities"

    id: str = Field(primary_key=True)
    discovered_at: str
    last_seen_at: str
    published_at: Optional[str] = Field(default=None)

    company: Optional[str] = Field(default=None, index=True)
    title: str
    original_url: str
    canonical_url: str = Field(index=True)
    content_fingerprint: Optional[str] = Field(default=None, index=True)
    primary_source: Optional[str] = Field(default=None, index=True)
    source_job_id: Optional[str] = Field(default=None)

    location: Optional[str] = Field(default=None)
    workplace_mode: Optional[str] = Field(default=None, index=True)
    contract_type: Optional[str] = Field(default=None)
    opportunity_type: str = Field(default="vaga", index=True)

    area_id: Optional[str] = Field(default=None, index=True)
    career_tier: Optional[str] = Field(default=None)
    classification: str = Field(index=True)
    professional_compatibility: Optional[str] = Field(default=None, index=True)
    operational_status: Optional[str] = Field(default=None)

    relevance_score: int = Field(default=0)
    practical_priority: int = Field(default=0)
    validity_status: str = Field(default="desconhecida")

    application_method: str = Field(default="desconhecido")
    application_effort: str = Field(default="desconhecido")
    automation_capability: str = Field(default="desconhecida")
    service_tier: Optional[int] = Field(default=None)
    application_stage: str = Field(default="descoberta", index=True)

    description_text: str = Field(default="")
    requirements_json: str = Field(default="[]")
    analysis_json: str = Field(default="{}")
    raw_payload_json: str = Field(default="{}")


class JobObservation(SQLModel, table=True):
    __tablename__ = "job_observations"

    id: Optional[int] = Field(default=None, primary_key=True)
    opportunity_id: str = Field(foreign_key="job_opportunities.id", index=True)
    observation_kind: str = Field(default="source_sighting", index=True)
    source_name: str = Field(index=True)
    source_item_id: Optional[str] = Field(default=None)
    canonical_url: str = Field(index=True)
    content_fingerprint: Optional[str] = Field(default=None, index=True)
    observed_at: str
    payload_json: str = Field(default="{}")


class ResumeVersion(SQLModel, table=True):
    __tablename__ = "resume_versions"

    id: str = Field(primary_key=True)
    created_at: str = Field(index=True)
    opportunity_id: Optional[str] = Field(
        default=None, foreign_key="job_opportunities.id", index=True
    )
    company: Optional[str] = Field(default=None)
    job_title: Optional[str] = Field(default=None)
    source_url: Optional[str] = Field(default=None)
    area_id: Optional[str] = Field(default=None, index=True)
    generation_mode: str = Field(default="deterministic")
    status: str = Field(index=True)

    description_sha256: str
    pdf_sha256: Optional[str] = Field(default=None, index=True)
    docx_sha256: Optional[str] = Field(default=None)
    previous_pdf_sha256: Optional[str] = Field(default=None)
    previous_docx_sha256: Optional[str] = Field(default=None)
    export_rel_path: Optional[str] = Field(default=None)

    rendered_payload_json: str = Field(default="{}")
    changes_json: str = Field(default="[]")
    error_message: Optional[str] = Field(default=None)


class ApplicationTracking(SQLModel, table=True):
    __tablename__ = "application_tracking"

    opportunity_id: str = Field(
        primary_key=True, foreign_key="job_opportunities.id"
    )
    updated_at: str
    stage: str = Field(index=True)
    queue_state: Optional[str] = Field(default=None, index=True)
    professional_compatibility: Optional[str] = Field(default=None)
    operational_status: Optional[str] = Field(default=None)
    service_tier: Optional[int] = Field(default=None)
    automation_capability: Optional[str] = Field(default=None)
    application_method: Optional[str] = Field(default=None)
    pdf_status: Optional[str] = Field(default=None)
    resume_version_id: Optional[str] = Field(
        default=None, foreign_key="resume_versions.id"
    )
    submission_instructions: Optional[str] = Field(default=None)
    user_confirmed_at: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    queue_assessment_json: Optional[str] = Field(default=None)


class ApplicationEvent(SQLModel, table=True):
    __tablename__ = "application_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    opportunity_id: str = Field(foreign_key="job_opportunities.id", index=True)
    created_at: str = Field(index=True)
    event_source: str = Field(default="tracking")
    stage: str
    notes: Optional[str] = Field(default=None)
    payload_json: Optional[str] = Field(default=None)


class DiscoveryRun(SQLModel, table=True):
    __tablename__ = "discovery_runs"

    id: str = Field(primary_key=True)
    run_kind: str = Field(default="discovery", index=True)
    started_at: str = Field(index=True)
    finished_at: str
    extracted_count: int = Field(default=0)
    unique_count: int = Field(default=0)
    new_count: int = Field(default=0)
    summary_json: str = Field(default="{}")
