/* Typed v2.1 Contracts (100% Portuguese-BR, Exclusive Google Connection, Unified MasterProfile v2.1) */

export type SectionKind =
  | 'summary'
  | 'experience'
  | 'education'
  | 'projects'
  | 'courses'
  | 'skills'
  | 'languages';

export type ResumeFontFamily =
  | 'Libre Franklin'
  | 'Inter'
  | 'Arial'
  | 'Georgia'
  | 'Times New Roman';

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
  scheduler_enabled: boolean;
  ai_connected: boolean;
  bootstrapped_demo: boolean;
  counts: {
    opportunities: number;
    resume_versions: number;
    tracked_applications: number;
    discovery_runs: number;
  };
}

/* ---------------- Jobs & Discovery ---------------- */

export interface JobHumanLabels {
  classification_label: string;
  compatibility_label: string;
  operational_label: string;
  automation_label: string;
  stage_label: string;
  workplace_label: string;
  career_tier_label: string;
  service_tier_label: string;
}

export interface JobListItem {
  id: string;
  company: string | null;
  title: string;
  original_url: string;
  canonical_url: string | null;
  primary_source?: string | null;
  source_label: string;
  location: string | null;
  workplace_mode: string | null;
  contract_type: string | null;
  area_id: string | null;
  classification: string;
  professional_compatibility: string;
  operational_status: string;
  service_tier: number;
  application_stage: string;
  published_at: string | null;
  published_ago: string;
  last_seen_at: string;
  labels: JobHumanLabels;
}

export interface JobDetailResponse {
  opportunity: JobListItem & {
    description_text?: string;
    discovered_at?: string;
  };
  requirements: Array<{
    skill_name: string;
    matched_keywords: string[];
    requirement_kind: string;
    excerpt: string;
  }>;
  analysis: {
    matched_skills?: string[];
    preparable_gaps?: string[];
    short_prep_plans?: Array<{ term: string; study_plan: string }>;
    operational_omissions?: string[];
    hard_incompatibilities?: string[];
    [key: string]: unknown;
  };
  tracking: Record<string, unknown> | null;
}

export interface AssistedImportPayload {
  url: string;
  title?: string;
  company?: string;
  location?: string;
  description: string;
  workplace_mode?: 'presencial' | 'hibrido' | 'remoto' | null;
}

export interface SchedulerStatus {
  enabled: boolean;
  scheduler_running?: boolean;
  interval_minutes: number;
  max_requests_budget: number;
  next_run_time?: string | null;
}

/* ---------------- Unified Base Resume (MasterProfile v2.1) ---------------- */

export interface ProfileLink {
  label: string;
  url: string;
  display_text: string;
}

export interface ExperienceEntry {
  id: string;
  role: string;
  company: string;
  period: string;
  location: string;
  description: string;
}

export interface EducationEntry {
  id: string;
  degree: string;
  institution: string;
  period: string;
  details: string;
}

export interface CourseEntry {
  id: string;
  title: string;
  institution: string;
  workload: string;
  completion_date: string;
  description: string;
}

export interface ProjectEntry {
  id: string;
  name: string;
  url: string | null;
  description: string;
}

export interface LanguageEntry {
  id: string;
  language: string;
  proficiency: string;
}

export interface MasterProfile {
  education_order_manual?: boolean;
  schema_version: '2.1';
  header: {
    full_name: string;
    email: string;
    phone: string;
    city: string;
    state: string;
    neighborhood: string | null;
    country: string;
    age: number | null;
    links: ProfileLink[];
  };
  summary: string;
  experiences: ExperienceEntry[];
  education: EducationEntry[];
  courses: CourseEntry[];
  projects: ProjectEntry[];
  skills: string[];
  languages: LanguageEntry[];
}

export interface ProfileImportResponse {
  extraction_mode: 'ai' | 'heuristic_fallback';
  provider: string | null;
  model: string | null;
  extracted_chars: number;
  warnings: string[];
  profile_draft: MasterProfile;
}

/* ---------------- Editable ResumeDraft & PDF Export ---------------- */

export interface SectionLabels {
  summary: string;
  experience: string;
  education: string;
  projects: string;
  courses: string;
  skills: string;
  languages: string;
}

export interface DraftExperienceItem {
  id: string;
  role: string;
  company: string;
  period: string;
  location: string;
  description: string;
  highlights: string[];
}

export interface DraftEducationItem {
  id: string;
  degree: string;
  institution: string;
  period: string;
  details: string;
}

export interface DraftCourseItem {
  id: string;
  title: string;
  institution: string;
  workload: string;
  completion_date: string;
  description: string;
}

export interface DraftProjectItem {
  id: string;
  name: string;
  url: string | null;
  description: string;
  highlights: string[];
}

export interface DraftSkillGroup {
  id: string;
  label: string;
  items: string[];
}

export interface DraftLanguageItem {
  id: string;
  language: string;
  proficiency: string;
}

export interface ResumeDraft {
  layout_step: number;
  target_role: string;
  company: string | null;
  max_pages: number;
  font_family: ResumeFontFamily;
  compact_mode: boolean;
  section_labels: SectionLabels;
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
  summary: string;
  experiences: DraftExperienceItem[];
  education: DraftEducationItem[];
  courses: DraftCourseItem[];
  projects: DraftProjectItem[];
  skill_groups: DraftSkillGroup[];
  languages: DraftLanguageItem[];
}

export interface GenerateResumeResponse {
  provider: string;
  model: string;
  token_usage: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
  job_context: {
    opportunity_id: string | null;
    job_title: string;
    company: string | null;
  };
  draft: ResumeDraft;
}

export interface ExportPdfResponse {
  version_id: string;
  opportunity_id?: string | null;
  job_url?: string | null;
  generation_mode: string;
  export_rel_path: string;
  output_pdf_filename: string;
  pdf_sha256: string;
  pdf_size_bytes: number;
  ats_validation: {
    passed: boolean;
    page_count: number;
    max_pages: number;
    clickable_uris: string[];
  };
  draft: ResumeDraft;
}

/* ---------------- Applications Tracking ---------------- */

export interface ApplicationCard {
  opportunity_id: string;
  updated_at: string;
  stage: KanbanStage;
  stage_label: string;
  labels: JobHumanLabels;
  notes: string | null;
  user_confirmed_at: string | null;
  job: {
    title: string;
    company: string | null;
    original_url: string;
    location: string | null;
    workplace_mode: string | null;
    area_id: string | null;
  };
  resume_version: {
    id: string;
    export_rel_path: string | null;
    created_at: string;
  } | null;
}

/* ---------------- Settings Drawer ---------------- */

export interface SearchCriteria {
  schema_version: '1.0';
  include_remote: boolean;
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
  contracts: Record<string, unknown>;
  career_focus: Record<string, unknown>;
  omissions_policy: Record<string, unknown>;
  role_catalog: Record<string, unknown>;
  short_prep_rules: Array<Record<string, unknown>>;
  strategic_exclusions: Array<Record<string, unknown>>;
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
  display_name?: string;
  category?: string;
  type: string;
  enabled: boolean;
  priority: 'high' | 'secondary';
  rate_limit_seconds: number;
  notes?: string | null;
}

export interface LlmConfigResponse {
  connected: boolean;
  model: string;
}
