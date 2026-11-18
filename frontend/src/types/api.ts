/* Typed REST Contracts matching FastAPI Pydantic v2 Models */

export type SupportedLocale = 'pt-BR' | 'en-US';
export type SectionKind =
  | 'summary'
  | 'education'
  | 'experience'
  | 'courses'
  | 'projects'
  | 'skills'
  | 'languages';

export type VeracityState =
  | 'confirmed_practice'
  | 'academic_foundation'
  | 'preparable_gap'
  | 'unconfirmed';

export type KanbanStage =
  | 'descoberta'
  | 'candidatura_a_preparar'
  | 'curriculo_preparado'
  | 'aguardando_envio_manual'
  | 'enviada_manualmente'
  | 'encerrada_ou_descartada';

export interface HealthResponse {
  status: string;
  project: string;
  version: string;
  sqlite_journal_mode: string;
  scheduler_enabled: boolean;
  llm_provider: 'none' | 'gemini' | 'groq' | 'ollama';
  bootstrapped_demo: boolean;
  counts: {
    opportunities: number;
    resume_versions: number;
    tracked_applications: number;
    discovery_runs: number;
  };
}

/* ---------------- Jobs & Discovery ---------------- */

export interface JobListItem {
  id: string;
  company: string | null;
  title: string;
  original_url: string;
  canonical_url: string | null;
  location: string | null;
  workplace_mode: string | null;
  contract_type: string | null;
  opportunity_type: string;
  area_id: string | null;
  career_tier: string | null;
  classification: string;
  professional_compatibility: string;
  operational_status: string;
  relevance_score: number;
  practical_priority: number;
  validity_status: string | null;
  application_method: string;
  application_effort: string;
  automation_capability: string;
  service_tier: number;
  application_stage: string;
  published_at: string | null;
  last_seen_at: string;
}

export interface MatchedRequirement {
  skill_id: string;
  skill_name: string;
  matched_keywords: string[];
  requirement_kind: string;
  excerpt: string;
  veracity_state: string;
  can_appear_on_resume: boolean;
  study_plan?: string | null;
}

export interface ShortPrepPlanItem {
  term: string;
  study_plan: string;
  acquired: boolean;
}

export interface TriageResult {
  classification: string;
  professional_compatibility: string;
  local_prep_allowed: boolean;
  area_id: string | null;
  candidate_areas: string[];
  area_scores: Record<string, number>;
  career_tier: string;
  relevance_score: number;
  practical_priority: number;
  hard_incompatibilities: string[];
  strategic_exclusions: string[];
  blocking_pendencies: string[];
  operational_omissions: string[];
  technical_review_notes: string[];
  operational_status: string;
  inferred_workplace_mode: string | null;
  inferred_contract: string | null;
  matched_city_slug: string | null;
  geography_state: string;
  matched_skills: string[];
  preparable_gaps: string[];
  unconfirmed_skills: string[];
  requirements?: MatchedRequirement[];
  short_prep_plans?: ShortPrepPlanItem[];
  service_tier?: number;
  automation_capability?: string;
  application_method?: string;
  application_effort?: string;
}

export interface JobDetailResponse {
  opportunity: JobListItem & {
    description_text?: string;
    discovered_at?: string;
  };
  requirements: MatchedRequirement[];
  analysis: Partial<TriageResult> & Record<string, unknown>;
  tracking: Record<string, unknown> | null;
  observations: Array<{
    id: number;
    observed_at: string;
    observation_kind: string;
    source_name: string;
    original_url?: string | null;
  }>;
}

export interface SchedulerStatus {
  enabled: boolean;
  running: boolean;
  interval_minutes: number;
  max_requests_budget: number;
  next_run_at: string | null;
  last_run_summary: Record<string, unknown> | null;
}

export interface AssistedImportPayload {
  url: string;
  title?: string;
  company?: string;
  location?: string;
  description: string;
  workplace_mode?: 'presencial' | 'hibrido' | 'remoto' | null;
  contract_hint?: string;
  pdf_confirmed?: boolean;
  access_blocked?: boolean;
  requires_login?: boolean;
}

/* ---------------- Resume Studio ---------------- */

export interface ProfileLink {
  label: string;
  url: string;
  display_text: string;
}

export interface RenderedBulletItem {
  id: string;
  text: string;
  score: number;
  factual_source: string;
}

export interface RenderedExperienceItem {
  id: string;
  title_company_bold: string;
  meta_suffix: string;
  bullets: RenderedBulletItem[];
  factual_source: string;
}

export interface RenderedSkillGroup {
  id: string;
  label: string;
  items_text: string;
  score: number;
  factual_source: string;
}

export interface ResumeDocumentPayload {
  area_id: string;
  display_name: string;
  locale: SupportedLocale;
  max_pages: number;
  compact_mode: boolean;
  output_folder: string;
  output_pdf_filename: string;
  section_labels: Record<SectionKind, string>;
  section_order: SectionKind[];
  page_break_before_sections: SectionKind[];
  header: {
    full_name: string;
    location_line: string;
    contact_line: string;
    email: string;
    phone: string;
    links: ProfileLink[];
  };
  summary: {
    text: string;
    factual_source: string;
  };
  education: Array<{
    id: string;
    degree_line: string;
    institution_line: string;
    period_line: string;
    status: 'completed' | 'in_progress';
    factual_source: string;
  }>;
  experiences: RenderedExperienceItem[];
  courses: Array<{
    id: string;
    entry_type: 'course' | 'practical_assessment';
    bold_prefix: string;
    rest_text: string;
    factual_source: string;
  }>;
  projects: Array<{
    id: string;
    name: string;
    summary?: string | null;
    url?: string | null;
    bullets: RenderedBulletItem[];
    factual_source: string;
  }>;
  skill_groups: RenderedSkillGroup[];
  languages: Array<{
    id: string;
    language: string;
    proficiency: string;
  }>;
  applied_equivalences: Array<{
    from_term: string;
    to_term: string;
    factual_basis: string;
  }>;
}

export interface DiffEntry {
  block_id: string;
  before: string;
  after: string;
  changed: boolean;
  reverted_to_deterministic: boolean;
}

export interface GuardWarning {
  block_id: string;
  reason: string;
  detail: string;
}

export interface SurgicalAdaptResponse {
  provider: string;
  model: string;
  token_usage: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
  guard_warnings: GuardWarning[];
  diff: DiffEntry[];
  payload: ResumeDocumentPayload;
  triage: TriageResult | null;
}

export interface ExportPdfResponse {
  version_id: string;
  area_id: string;
  locale: SupportedLocale;
  generation_mode: 'deterministic' | 'surgical_ai';
  export_rel_path: string;
  output_pdf_filename: string;
  pdf_sha256: string;
  pdf_size_bytes: number;
  ats_validation: {
    valid: boolean;
    page_count: number;
    max_pages: number;
    extracted_char_count: number;
    clickable_uris: string[];
    errors: string[];
  };
  payload: ResumeDocumentPayload;
}

export interface ResumeVersionRecord {
  id: string;
  created_at: string;
  opportunity_id: string | null;
  company: string | null;
  job_title: string | null;
  area_id: string;
  generation_mode: string;
  status: string;
  pdf_sha256: string | null;
  export_rel_path: string | null;
}

/* ---------------- Kanban Applications ---------------- */

export interface ApplicationCard {
  opportunity_id: string;
  updated_at: string;
  stage: KanbanStage;
  queue_state: string | null;
  professional_compatibility: string | null;
  operational_status: string | null;
  service_tier: number;
  automation_capability: string | null;
  application_method: string | null;
  pdf_status: string | null;
  submission_instructions: string | null;
  user_confirmed_at: string | null;
  notes: string | null;
  job: {
    title: string;
    company: string | null;
    original_url: string;
    location: string | null;
    workplace_mode: string | null;
    area_id: string | null;
    classification: string;
    practical_priority: number;
  };
  resume_version: {
    id: string;
    area_id: string;
    generation_mode: string;
    pdf_sha256: string | null;
    export_rel_path: string | null;
    created_at: string;
  } | null;
}

export interface ApplicationEventItem {
  id: number;
  opportunity_id: string;
  created_at: string;
  event_source: string;
  stage: string;
  notes: string | null;
}

/* ---------------- Master Profile & Settings ---------------- */

export interface BulletItem {
  id: string;
  text: string;
  areas: string[];
  skill_tags: string[];
  keywords: string[];
  factual_source: string;
  translations: Partial<Record<SupportedLocale, string>>;
}

export interface ExperienceEntry {
  id: string;
  role: string;
  company: string;
  employment_type_label?: string | null;
  period_label: string;
  bullets: BulletItem[];
  factual_source: string;
  translations: Partial<
    Record<
      SupportedLocale,
      { role: string; employment_type_label?: string | null; period_label: string }
    >
  >;
}

export interface SkillItem {
  id: string;
  display_name: string;
  aliases: string[];
  areas: string[];
  veracity: VeracityState;
  internal_context?: string | null;
  summary_label?: string | null;
  study_plan?: string | null;
  factual_source?: string | null;
  translations: Partial<Record<SupportedLocale, string>>;
}

export interface EducationEntry {
  id: string;
  degree: string;
  institution: string;
  program_note?: string | null;
  period_label: string;
  status: 'completed' | 'in_progress';
  areas: string[];
  factual_source: string;
  translations: Record<string, unknown>;
}

export interface CourseEntry {
  id: string;
  entry_type: 'course' | 'practical_assessment';
  title: string;
  institution: string;
  details_label: string;
  hours?: number | null;
  year?: number | null;
  status: 'completed' | 'in_progress_free' | 'planned';
  areas: string[];
  factual_source: string;
  translations: Record<string, unknown>;
}

export interface ProjectEntry {
  id: string;
  name: string;
  status: 'approved' | 'reserved';
  summary?: string | null;
  bullets: BulletItem[];
  areas: string[];
  url?: string | null;
  factual_source: string;
}

export interface AreaPreset {
  area_id: string;
  display_name: string;
  locale: SupportedLocale;
  output_folder: string;
  output_pdf_filename: string;
  show_age: boolean;
  show_neighborhood: boolean;
  max_pages: number;
  section_labels: Record<SectionKind, string>;
  section_order: SectionKind[];
  page_break_before_sections: SectionKind[];
  default_summary: string;
  summary_prefix: string;
  allowed_summary_skill_ids: string[];
  preferred_bullet_ids: string[];
  max_bullets_per_experience: number;
  included_course_ids: string[];
  included_project_ids: string[];
  skill_groups: Array<{
    id: string;
    label: string;
    items_text: string;
    skill_ids: string[];
    factual_source: string;
  }>;
}

export interface MasterProfile {
  schema_version: '1.0';
  header: {
    full_name: string;
    age?: number | null;
    neighborhood?: string | null;
    city: string;
    state: string;
    country_by_locale: Record<SupportedLocale, string>;
    email: string;
    phone: string;
    links: ProfileLink[];
  };
  education: EducationEntry[];
  experiences: ExperienceEntry[];
  courses: CourseEntry[];
  projects: ProjectEntry[];
  skills: SkillItem[];
  languages: Array<{
    id: string;
    language: string;
    proficiency: string;
    translations: Record<string, unknown>;
  }>;
  contextual_equivalences: Array<{
    from_term: string;
    to_term: string;
    trigger_terms: string[];
    factual_basis: string;
  }>;
  forbidden_resume_terms: string[];
  area_presets: Record<string, AreaPreset>;
}

export interface SearchCriteria {
  schema_version: '1.0';
  geography: {
    allowed_cities: Array<{
      slug: string;
      canonical_name: string;
      state_code: string;
      aliases: string[];
      neighborhoods: string[];
    }>;
    ambiguous_neighborhoods_without_city: string[];
    accept_remote_national: boolean;
    accept_remote_international_if_resident_allowed: boolean;
    reject_broader_metro_if_outside_allowed_cities: boolean;
  };
  schedule: {
    earliest_hour: number;
    latest_hour: number;
    soft_latest_hour: number;
    reject_night_or_overnight: boolean;
  };
  contracts: {
    accepted_types: string[];
    rejected_types: string[];
    require_explicit_paid_internship_signal: boolean;
    allow_undisclosed_salary: boolean;
  };
  career_focus: {
    entry_level_signals: string[];
    out_of_focus_seniority_signals: string[];
    max_mandatory_experience_months: number;
    preferred_recency_days: number;
    equal_area_priority: boolean;
    max_secondary_sources_per_run: number;
  };
  omissions_policy: {
    allow_local_prep_with_operational_omissions: boolean;
    never_assume_unconfirmed_remote_or_paid: boolean;
  };
  role_catalog: {
    area_keywords: Record<string, string[]>;
    informal_hiring_signals: string[];
    grouped_search_queries: string[];
  };
  short_prep_rules: Array<{
    term: string;
    aliases: string[];
    prerequisite_skill_ids: string[];
    study_plan: string;
  }>;
  strategic_exclusions: Array<{
    id: string;
    urls: string[];
    excluded_source_terms: string[];
    reason: string;
  }>;
}

export interface BlockedCompanyEntry {
  id: string;
  display_name: string;
  permanent: boolean;
  cnpj?: string | null;
  corporate_name?: string | null;
  domains: string[];
  aliases: string[];
  confirmed_related_entities: Array<{
    name: string;
    relationship_type: 'corporate_group' | 'shared_ownership' | 'recruiting_on_behalf';
    evidence_source: string;
  }>;
  blocked_actions: string[];
  require_evidence_for_relations: boolean;
  uncertain_relation_policy: 'hold_for_manual_review';
  preserve_in_work_history: boolean;
}

export interface BlockedCompaniesConfig {
  schema_version: '1.0';
  companies: BlockedCompanyEntry[];
}

export interface JobSourceItem {
  id: string;
  type: string;
  enabled: boolean;
  priority: 'high' | 'secondary';
  rate_limit_seconds: number;
  notes?: string | null;
  [key: string]: unknown;
}

export interface LlmConfigResponse {
  provider: 'none' | 'gemini' | 'groq' | 'ollama';
  timeout_seconds: number;
  gemini_model: string;
  gemini_key_configured: boolean;
  groq_model: string;
  groq_key_configured: boolean;
  ollama_base_url: string;
  ollama_model: string;
}
