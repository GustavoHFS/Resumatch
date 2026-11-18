/* Lightweight Fetch API Client (Zero External HTTP Dependencies) */

import type {
  ApplicationCard,
  ApplicationEventItem,
  AssistedImportPayload,
  BlockedCompaniesConfig,
  ExportPdfResponse,
  HealthResponse,
  JobDetailResponse,
  JobListItem,
  JobSourceItem,
  LlmConfigResponse,
  MasterProfile,
  ResumeDocumentPayload,
  ResumeVersionRecord,
  SchedulerStatus,
  SearchCriteria,
  SectionKind,
  SurgicalAdaptResponse,
  TriageResult,
} from '../types/api';

export class ApiClientError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiClientError';
    this.status = status;
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    let detail = `Erro HTTP ${response.status}`;
    try {
      const errBody = (await response.json()) as { detail?: unknown };
      if (typeof errBody.detail === 'string') {
        detail = errBody.detail;
      } else if (Array.isArray(errBody.detail)) {
        detail = errBody.detail
          .map((item) => {
            if (typeof item === 'object' && item !== null && 'msg' in item) {
              const loc = Array.isArray((item as { loc?: unknown }).loc)
                ? ((item as { loc: unknown[] }).loc.join('.') + ': ')
                : '';
              return `${loc}${String((item as { msg: unknown }).msg)}`;
            }
            return JSON.stringify(item);
          })
          .join(' | ');
      }
    } catch {
      // fallback to generic HTTP status
    }
    throw new ApiClientError(detail, response.status);
  }

  return (await response.json()) as T;
}

export const api = {
  getHealth: () => requestJson<HealthResponse>('/api/health'),

  /* Jobs, Discovery & Scheduler */
  getJobs: (params: Record<string, string | number | undefined>) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== '') {
        qs.set(k, String(v));
      }
    });
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return requestJson<{ total: number; items: JobListItem[] }>(`/api/jobs${suffix}`);
  },

  getJobDetail: (opportunityId: string) =>
    requestJson<JobDetailResponse>(`/api/jobs/${encodeURIComponent(opportunityId)}`),

  importAssistedJob: (payload: AssistedImportPayload) =>
    requestJson<{
      action: string;
      opportunity_id: string;
      triage: TriageResult;
    }>('/api/jobs/import', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  runDiscoveryNow: (maxRequestsBudget = 40) =>
    requestJson<{
      run_id: number;
      extracted_count: number;
      unique_count: number;
      new_count: number;
      updated_count: number;
      requests_used: number;
    }>('/api/discovery/run', {
      method: 'POST',
      body: JSON.stringify({ max_requests_budget: maxRequestsBudget }),
    }),

  getScheduler: () => requestJson<SchedulerStatus>('/api/scheduler'),

  updateScheduler: (payload: {
    enabled: boolean;
    interval_minutes?: number;
    max_requests_budget?: number;
  }) =>
    requestJson<SchedulerStatus>('/api/scheduler', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /* Resume Studio */
  composeResume: (payload: {
    area_id: string;
    opportunity_id?: string | null;
    job_title?: string;
    company?: string;
    job_description?: string;
    section_order_override?: SectionKind[];
    payload_override?: ResumeDocumentPayload | null;
  }) =>
    requestJson<{
      elapsed_ms: number;
      payload: ResumeDocumentPayload;
      triage: TriageResult | null;
    }>('/api/resumes/compose', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  adaptResumeSurgical: (payload: {
    area_id: string;
    opportunity_id?: string | null;
    job_title?: string;
    company?: string;
    job_description?: string;
    section_order_override?: SectionKind[];
    provider_override?: 'gemini' | 'groq' | 'ollama' | null;
    extra_job_keywords?: string[];
  }) =>
    requestJson<SurgicalAdaptResponse>('/api/resumes/adapt', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  renderResumeHtml: (payload: {
    area_id: string;
    opportunity_id?: string | null;
    job_title?: string;
    company?: string;
    job_description?: string;
    section_order_override?: SectionKind[];
    standalone_preview?: boolean;
    payload_override?: ResumeDocumentPayload | null;
  }) =>
    requestJson<{
      area_id: string;
      locale: string;
      max_pages: number;
      html: string;
      payload: ResumeDocumentPayload;
    }>('/api/resumes/render-html', {
      method: 'POST',
      body: JSON.stringify({ ...payload, standalone_preview: true }),
    }),

  exportResumePdf: (payload: {
    area_id: string;
    opportunity_id?: string | null;
    job_title?: string;
    company?: string;
    job_description?: string;
    section_order_override?: SectionKind[];
    generation_mode: 'deterministic' | 'surgical_ai';
    auto_fit: boolean;
    payload_override?: ResumeDocumentPayload | null;
  }) =>
    requestJson<ExportPdfResponse>('/api/resumes/export-pdf', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  listResumeVersions: (params?: { area_id?: string; opportunity_id?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.area_id) qs.set('area_id', params.area_id);
    if (params?.opportunity_id) qs.set('opportunity_id', params.opportunity_id);
    if (params?.limit) qs.set('limit', String(params.limit));
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return requestJson<ResumeVersionRecord[]>(`/api/resumes/versions${suffix}`);
  },

  /* Kanban Applications */
  listApplications: (params?: { stage?: string; area_id?: string; search?: string }) => {
    const qs = new URLSearchParams();
    if (params?.stage) qs.set('stage', params.stage);
    if (params?.area_id) qs.set('area_id', params.area_id);
    if (params?.search) qs.set('search', params.search);
    qs.set('limit', '200');
    return requestJson<{ total: number; items: ApplicationCard[] }>(
      `/api/applications?${qs.toString()}`
    );
  },

  enqueueApplication: (payload: {
    opportunity_id: string;
    stage?: 'descoberta' | 'candidatura_a_preparar';
    notes?: string;
  }) =>
    requestJson<ApplicationCard>('/api/applications', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  transitionStage: (
    opportunityId: string,
    payload: {
      stage: string;
      resume_version_id?: string | null;
      confirm_manual_submission?: boolean;
      notes?: string;
    }
  ) =>
    requestJson<ApplicationCard>(
      `/api/applications/${encodeURIComponent(opportunityId)}/stage`,
      {
        method: 'PATCH',
        body: JSON.stringify(payload),
      }
    ),

  listApplicationEvents: (opportunityId: string) =>
    requestJson<ApplicationEventItem[]>(
      `/api/applications/${encodeURIComponent(opportunityId)}/events`
    ),

  /* Master Profile & Configurations */
  getProfile: () => requestJson<MasterProfile>('/api/profile'),

  saveProfile: (profile: MasterProfile) =>
    requestJson<{ status: string; backup_snapshot: string | null; profile: MasterProfile }>(
      '/api/profile',
      {
        method: 'PUT',
        body: JSON.stringify(profile),
      }
    ),

  getCriteria: () => requestJson<SearchCriteria>('/api/config/criteria'),

  saveCriteria: (criteria: SearchCriteria) =>
    requestJson<{ status: string; backup_snapshot: string | null; criteria: SearchCriteria }>(
      '/api/config/criteria',
      {
        method: 'PUT',
        body: JSON.stringify(criteria),
      }
    ),

  getBlocked: () => requestJson<BlockedCompaniesConfig>('/api/config/blocked'),

  saveBlocked: (blocked: BlockedCompaniesConfig) =>
    requestJson<{
      status: string;
      backup_snapshot: string | null;
      blocked: BlockedCompaniesConfig;
    }>('/api/config/blocked', {
      method: 'PUT',
      body: JSON.stringify(blocked),
    }),

  getSources: () => requestJson<{ schema_version: string; sources: JobSourceItem[] }>('/api/sources'),

  toggleSource: (sourceId: string, enabled: boolean) =>
    requestJson<{ id: string; enabled: boolean }>(
      `/api/sources/${encodeURIComponent(sourceId)}`,
      {
        method: 'PATCH',
        body: JSON.stringify({ enabled }),
      }
    ),

  getLlmConfig: () => requestJson<LlmConfigResponse>('/api/config/llm'),

  saveLlmConfig: (payload: {
    provider: 'none' | 'gemini' | 'groq' | 'ollama';
    timeout_seconds: number;
    gemini_model: string;
    gemini_api_key?: string;
    clear_gemini_key?: boolean;
    groq_model: string;
    groq_api_key?: string;
    clear_groq_key?: boolean;
    ollama_base_url: string;
    ollama_model: string;
  }) =>
    requestJson<{ status: string; backup_snapshot: string | null; llm: LlmConfigResponse }>(
      '/api/config/llm',
      {
        method: 'PUT',
        body: JSON.stringify(payload),
      }
    ),
};
