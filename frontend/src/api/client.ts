/* Resilient Fetch Client & Defensive Schema Normalizers for Resumatch v2.1 */

import type {
  ApplicationCard,
  AssistedImportPayload,
  BlockedCompaniesConfig,
  CourseEntry,
  EducationEntry,
  ExperienceEntry,
  ExportPdfResponse,
  GenerateResumeResponse,
  HealthResponse,
  JobDetailResponse,
  JobListItem,
  JobSourceItem,
  LanguageEntry,
  LlmConfigResponse,
  MasterProfile,
  ProfileImportResponse,
  ProfileLink,
  ProjectEntry,
  ResumeDraft,
  ResumeFontFamily,
  SchedulerStatus,
  SearchCriteria,
} from '../types/api';

export const SERVER_DISCONNECTED_MESSAGE =
  'Servidor local desconectado. Feche e abra o Resumatch novamente.';

export class ApiClientError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiClientError';
    this.status = status;
  }
}

const NETWORK_ERROR_PATTERN =
  /failed to fetch|networkerror|network error|load failed|fetch failed|err_connection|network request failed|connection refused/i;

export function isServerDisconnectedError(err: unknown): boolean {
  if (err instanceof ApiClientError) {
    return err.status === 0 || err.message === SERVER_DISCONNECTED_MESSAGE;
  }
  if (err instanceof Error) {
    return NETWORK_ERROR_PATTERN.test(err.message);
  }
  return false;
}

export function createEmptyMasterProfile(): MasterProfile {
  return {
    schema_version: '2.1',
    header: {
      full_name: '',
      email: '',
      phone: '',
      city: '',
      state: '',
      neighborhood: null,
      country: 'Brasil',
      age: null,
      links: [],
    },
    summary: '',
    experiences: [],
    education: [],
    courses: [],
    projects: [],
    skills: [],
    languages: [],
  };
}

export function normalizeMasterProfile(raw: unknown): MasterProfile {
  const fallback = createEmptyMasterProfile();
  if (!raw || typeof raw !== 'object') {
    return fallback;
  }
  const obj = raw as Record<string, unknown>;
  const hdr =
    obj.header && typeof obj.header === 'object'
      ? (obj.header as Record<string, unknown>)
      : {};

  const links: ProfileLink[] = Array.isArray(hdr.links)
    ? hdr.links
        .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
        .map((lnk) => ({
          label: String(lnk.label ?? 'Link'),
          url: String(lnk.url ?? ''),
          display_text: String(lnk.display_text ?? lnk.url ?? ''),
        }))
    : [];

  const experiences: ExperienceEntry[] = Array.isArray(obj.experiences)
    ? obj.experiences
        .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
        .map((exp, idx) => ({
          id: String(exp.id ?? `exp_${idx + 1}`),
          role: String(exp.role ?? ''),
          company: String(exp.company ?? ''),
          period: String(exp.period ?? ''),
          location: String(exp.location ?? ''),
          description: String(exp.description ?? ''),
        }))
    : [];

  const education: EducationEntry[] = Array.isArray(obj.education)
    ? obj.education
        .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
        .map((edu, idx) => ({
          id: String(edu.id ?? `edu_${idx + 1}`),
          degree: String(edu.degree ?? ''),
          institution: String(edu.institution ?? ''),
          period: String(edu.period ?? ''),
          details: String(edu.details ?? ''),
        }))
    : [];

  const courses: CourseEntry[] = Array.isArray(obj.courses)
    ? obj.courses
        .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
        .map((crs, idx) => ({
          id: String(crs.id ?? `crs_${idx + 1}`),
          title: String(crs.title ?? ''),
          institution: String(crs.institution ?? ''),
          workload: String(crs.workload ?? crs.workload_or_year ?? ''),
          completion_date: String(crs.completion_date ?? ''),
          description: String(crs.description ?? ''),
        }))
    : [];

  const projects: ProjectEntry[] = Array.isArray(obj.projects)
    ? obj.projects
        .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
        .map((prj, idx) => ({
          id: String(prj.id ?? `prj_${idx + 1}`),
          name: String(prj.name ?? ''),
          url: typeof prj.url === 'string' && prj.url.trim() ? prj.url.trim() : null,
          description: String(prj.description ?? ''),
        }))
    : [];

  const skills: string[] = Array.isArray(obj.skills)
    ? obj.skills
        .map((sk) => (typeof sk === 'string' ? sk.trim() : ''))
        .filter(Boolean)
    : [];

  const languages: LanguageEntry[] = Array.isArray(obj.languages)
    ? obj.languages
        .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
        .map((lng, idx) => ({
          id: String(lng.id ?? `lng_${idx + 1}`),
          language: String(lng.language ?? ''),
          proficiency: String(lng.proficiency ?? ''),
        }))
    : [];

  return {
    schema_version: '2.1',
    header: {
      full_name: String(hdr.full_name ?? fallback.header.full_name),
      email: String(hdr.email ?? ''),
      phone: String(hdr.phone ?? ''),
      city: String(hdr.city ?? ''),
      state: String(hdr.state ?? ''),
      neighborhood:
        typeof hdr.neighborhood === 'string' && hdr.neighborhood.trim()
          ? hdr.neighborhood
          : null,
      country: String(hdr.country ?? 'Brasil'),
      age: typeof hdr.age === 'number' ? hdr.age : null,
      links,
    },
    summary: String(obj.summary ?? ''),
    experiences,
    education,
    courses,
    projects,
    skills,
    languages,
  };
}

async function parseErrorDetail(response: Response): Promise<string> {
  let detail = `Erro (${response.status})`;
  try {
    const errBody = (await response.json()) as { detail?: unknown };
    if (typeof errBody.detail === 'string') {
      detail = errBody.detail;
    } else if (Array.isArray(errBody.detail)) {
      detail = errBody.detail
        .map((item) => {
          if (typeof item === 'object' && item !== null && 'msg' in item) {
            return String((item as { msg: unknown }).msg);
          }
          return JSON.stringify(item);
        })
        .join(' · ');
    }
  } catch {
    // ignore malformed error body
  }
  return detail;
}

async function requestJson<T>(
  path: string,
  init?: RequestInit,
  timeoutMs = 15000
): Promise<T> {
  const controller = new AbortController();
  const externalSignal = init?.signal;
  const onExternalAbort = () => controller.abort();
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort();
    } else {
      externalSignal.addEventListener('abort', onExternalAbort, { once: true });
    }
  }
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);

  const isFormData = init?.body instanceof FormData;
  try {
    let response: Response;
    for (let attempt = 0; ; attempt++) {
      try {
        response = await fetch(path, {
          ...init,
          signal: controller.signal,
          headers: isFormData
            ? init?.headers
            : { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
        });
        break;
      } catch (err) {
        if (controller.signal.aborted || attempt >= 1 || !NETWORK_ERROR_PATTERN.test(
          err instanceof Error ? err.message : String(err)
        )) throw err;
        await new Promise<void>((resolve, reject) => {
          const timer = window.setTimeout(() => {
            controller.signal.removeEventListener('abort', onAbort);
            resolve();
          }, 400);
          const onAbort = () => {
            window.clearTimeout(timer);
            reject(new DOMException('Cancelado', 'AbortError'));
          };
          controller.signal.addEventListener('abort', onAbort, { once: true });
        });
      }
    }

    if (!response.ok) {
      throw new ApiClientError(await parseErrorDetail(response), response.status);
    }
    window.dispatchEvent(new Event('resumatch:api-connected'));
    return (await response.json()) as T;
  } catch (err) {
    if (err instanceof ApiClientError) {
      throw err;
    }
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiClientError('Requisição cancelada ou tempo limite excedido.', 408);
    }
    const rawMessage = err instanceof Error ? err.message : String(err ?? '');
    if (NETWORK_ERROR_PATTERN.test(rawMessage)) {
      throw new ApiClientError(SERVER_DISCONNECTED_MESSAGE, 0);
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
    if (externalSignal) {
      externalSignal.removeEventListener('abort', onExternalAbort);
    }
  }
}

export const api = {
  getHealth: () => requestJson<HealthResponse>('/api/health', undefined, 8000),

  pingLifecycle: (clientId: string) =>
    requestJson<{ status: string }>('/api/lifecycle/ping', {
      method: 'POST',
      body: JSON.stringify({ client_id: clientId }),
    }, 4000),

  /* Vagas */
  getJobs: (params: Record<string, string | number | boolean | undefined>, signal?: AbortSignal) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== '') qs.set(k, String(v));
    });
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return requestJson<{ total: number; items: JobListItem[] }>(`/api/jobs${suffix}`, { signal });
  },

  getJobDetail: (opportunityId: string) =>
    requestJson<JobDetailResponse>(`/api/jobs/${encodeURIComponent(opportunityId)}`),

  importAssistedJob: (payload: AssistedImportPayload) =>
    requestJson<{ opportunity_id: string; created: boolean }>('/api/jobs/import', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  runDiscoveryNow: (maxRequestsBudget = 80) =>
    requestJson<{
      extracted_count: number;
      unique_count: number;
      new_count: number;
      discarded_older_than_14d?: number;
    }>(
      '/api/discovery/run',
      {
        method: 'POST',
        body: JSON.stringify({ max_requests_budget: maxRequestsBudget }),
      },
      60000
    ),

  /* Currículo Base */
  getProfile: async (): Promise<MasterProfile> => {
    const raw = await requestJson<unknown>('/api/profile');
    return normalizeMasterProfile(raw);
  },

  saveProfile: async (profile: MasterProfile): Promise<{ status: string; profile: MasterProfile }> => {
    const res = await requestJson<{ status: string; profile: unknown }>('/api/profile', {
      method: 'PUT',
      body: JSON.stringify(normalizeMasterProfile(profile)),
    });
    return { status: res.status, profile: normalizeMasterProfile(res.profile) };
  },

  importProfileFile: async (file: File): Promise<ProfileImportResponse> => {
    const formData = new FormData();
    formData.append('file', file);
    const res = await requestJson<ProfileImportResponse>(
      '/api/profile/import-file',
      {
        method: 'POST',
        body: formData,
      },
      45000
    );
    return {
      ...res,
      warnings: Array.isArray(res.warnings) ? res.warnings : [],
      profile_draft: normalizeMasterProfile(res.profile_draft),
    };
  },

  /* Estúdio A4 (Google Gemini + Draft Editável) */
  generateResume: (payload: {
    opportunity_id?: string | null;
    job_title?: string;
    company?: string | null;
    job_description?: string;
    max_pages?: number;
    font_family?: ResumeFontFamily;
  }) =>
    requestJson<GenerateResumeResponse>(
      '/api/resumes/generate',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      45000
    ),

  renderResumeHtml: (draft: ResumeDraft, signal?: AbortSignal) =>
    requestJson<{ max_pages: number; html: string; draft: ResumeDraft }>(
      '/api/resumes/render-html',
      {
        method: 'POST',
        body: JSON.stringify({ draft, standalone_preview: true }),
        signal,
      }
    ),

  exportResumePdf: (payload: {
    draft: ResumeDraft;
    opportunity_id?: string | null;
    auto_fit?: boolean;
    manual_job_description?: string;
    manual_job_url?: string;
  }) =>
    requestJson<ExportPdfResponse>(
      '/api/resumes/export-pdf',
      {
        method: 'POST',
        body: JSON.stringify({ auto_fit: true, ...payload }),
      },
      35000
    ),

  /* Candidaturas */
  listApplications: () =>
    requestJson<{ total: number; items: ApplicationCard[] }>('/api/applications?limit=200'),

  enqueueApplication: (opportunityId: string) =>
    requestJson<ApplicationCard>('/api/applications', {
      method: 'POST',
      body: JSON.stringify({
        opportunity_id: opportunityId,
        stage: 'candidatura_a_preparar',
      }),
    }),

  transitionStage: (
    opportunityId: string,
    payload: {
      stage: string;
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

  deleteApplication: (opportunityId: string) =>
    requestJson<{ status: string }>(
      `/api/applications/${encodeURIComponent(opportunityId)}`,
      { method: 'DELETE' }
    ),

  /* Configurações */
  getScheduler: () => requestJson<SchedulerStatus>('/api/scheduler'),

  updateScheduler: (payload: { enabled: boolean; interval_minutes?: number }) =>
    requestJson<SchedulerStatus>('/api/scheduler', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getCriteria: () => requestJson<SearchCriteria>('/api/config/criteria'),

  saveCriteria: (criteria: SearchCriteria) =>
    requestJson<{ status: string; criteria: SearchCriteria }>('/api/config/criteria', {
      method: 'PUT',
      body: JSON.stringify(criteria),
    }),

  getBlocked: () => requestJson<BlockedCompaniesConfig>('/api/config/blocked'),

  saveBlocked: (blocked: BlockedCompaniesConfig) =>
    requestJson<{ status: string; blocked: BlockedCompaniesConfig }>('/api/config/blocked', {
      method: 'PUT',
      body: JSON.stringify(blocked),
    }),

  getSources: async (): Promise<{ sources: JobSourceItem[] }> => {
    const raw = await requestJson<unknown>('/api/sources');
    if (Array.isArray(raw)) {
      return { sources: raw as JobSourceItem[] };
    }
    if (raw && typeof raw === 'object' && Array.isArray((raw as { sources?: unknown }).sources)) {
      return { sources: (raw as { sources: JobSourceItem[] }).sources };
    }
    return { sources: [] };
  },

  toggleSource: (sourceId: string, enabled: boolean) =>
    requestJson<{ id: string; enabled: boolean }>(
      `/api/sources/${encodeURIComponent(sourceId)}`,
      {
        method: 'PATCH',
        body: JSON.stringify({ enabled }),
      }
    ),

  getLlmConfig: () => requestJson<LlmConfigResponse>('/api/config/llm'),

  connectGoogleAi: (apiKey: string) =>
    requestJson<{ status: string; llm: LlmConfigResponse }>(
      '/api/config/llm/connect',
      {
        method: 'POST',
        body: JSON.stringify({ api_key: apiKey }),
      },
      25000
    ),

  disconnectGoogleAi: () =>
    requestJson<{ status: string; llm: LlmConfigResponse }>('/api/config/llm', {
      method: 'DELETE',
    }),
};
