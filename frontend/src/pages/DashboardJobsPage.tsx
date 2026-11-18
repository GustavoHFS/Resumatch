import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type {
  AreaPreset,
  AssistedImportPayload,
  HealthResponse,
  JobDetailResponse,
  JobListItem,
  SchedulerStatus,
} from '../types/api';
import styles from './DashboardJobsPage.module.css';

interface Props {
  health: HealthResponse | null;
  areaPresets: Record<string, AreaPreset>;
  onRefreshHealth: () => void;
  onOpenInStudio: (job: JobListItem, suggestedAreaId?: string | null) => void;
}

const COMPATIBILITY_LABELS: Record<string, { label: string; cls: string }> = {
  compativel: { label: 'Eixo 1: Compatível', cls: 'badgeOk' },
  compativel_com_preparacao: { label: 'Eixo 1: Compatível c/ Preparo', cls: 'badgeWarn' },
  insuficiente_para_avaliar: { label: 'Eixo 1: Info Insuficiente', cls: 'badgeNeutral' },
  incompativel_por_impedimento_concreto: { label: 'Eixo 1: Incompatível', cls: 'badgeDanger' },
};

const OPERATIONAL_LABELS: Record<string, { label: string; cls: string }> = {
  pronta_para_preparar_candidatura: { label: 'Eixo 2: Pronta p/ Preparo', cls: 'badgeOk' },
  necessita_confirmar_informacao_importante: {
    label: 'Eixo 2: Omissão Operacional',
    cls: 'badgeWarn',
  },
  necessita_intervencao_usuario: { label: 'Eixo 2: Requer Intervenção', cls: 'badgeInfo' },
  encerrada_ou_indisponivel: { label: 'Eixo 2: Encerrada', cls: 'badgeDanger' },
};

const TIER_LABELS: Record<number, { label: string; cls: string }> = {
  1: { label: 'Eixo 3: Nível 1 (Direto)', cls: 'badgeInfo' },
  2: { label: 'Eixo 3: Nível 2 (Assistido)', cls: 'badgePurple' },
  3: { label: 'Eixo 3: Nível 3 (Portal Login)', cls: 'badgeNeutral' },
};

export const DashboardJobsPage: React.FC<Props> = ({
  health,
  areaPresets,
  onRefreshHealth,
  onOpenInStudio,
}) => {
  const [jobs, setJobs] = useState<JobListItem[]>([]);
  const [totalJobs, setTotalJobs] = useState(0);
  const [loadingJobs, setLoadingJobs] = useState(false);

  const [filterSearch, setFilterSearch] = useState('');
  const [filterArea, setFilterArea] = useState('');
  const [filterCompatibility, setFilterCompatibility] = useState('');
  const [filterTier, setFilterTier] = useState('');

  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [jobDetail, setJobDetail] = useState<JobDetailResponse | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [schedInterval, setSchedInterval] = useState(360);
  const [runningDiscovery, setRunningDiscovery] = useState(false);
  const [feedback, setFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  const [showImportModal, setShowImportModal] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importForm, setImportForm] = useState<AssistedImportPayload>({
    url: '',
    title: '',
    company: '',
    location: '',
    description: '',
    workplace_mode: null,
    contract_hint: '',
    requires_login: false,
  });

  const loadJobs = useCallback(async () => {
    setLoadingJobs(true);
    try {
      const res = await api.getJobs({
        search: filterSearch || undefined,
        area_id: filterArea || undefined,
        compatibility: filterCompatibility || undefined,
        service_tier: filterTier ? Number(filterTier) : undefined,
        limit: 80,
      });
      setJobs(res.items);
      setTotalJobs(res.total);
      if (res.items.length > 0 && !selectedJobId) {
        setSelectedJobId(res.items[0].id);
      }
    } catch (err) {
      setFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Falha ao carregar vagas.',
      });
    } finally {
      setLoadingJobs(false);
    }
  }, [filterSearch, filterArea, filterCompatibility, filterTier, selectedJobId]);

  const loadScheduler = useCallback(async () => {
    try {
      const st = await api.getScheduler();
      setScheduler(st);
      setSchedInterval(st.interval_minutes);
    } catch {
      // non-blocking
    }
  }, []);

  useEffect(() => {
    void loadJobs();
  }, [loadJobs]);

  useEffect(() => {
    void loadScheduler();
  }, [loadScheduler]);

  useEffect(() => {
    if (!selectedJobId) {
      setJobDetail(null);
      return;
    }
    let cancelled = false;
    setLoadingDetail(true);
    api
      .getJobDetail(selectedJobId)
      .then((detail) => {
        if (!cancelled) setJobDetail(detail);
      })
      .catch(() => {
        if (!cancelled) setJobDetail(null);
      })
      .finally(() => {
        if (!cancelled) setLoadingDetail(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedJobId]);

  const handleRunDiscovery = async () => {
    setRunningDiscovery(true);
    setFeedback(null);
    try {
      const summary = await api.runDiscoveryNow(40);
      setFeedback({
        type: 'ok',
        text: `Busca determinística concluída: ${summary.extracted_count} extraídas, ${summary.unique_count} únicas, ${summary.new_count} novas (${summary.requests_used} reqs HTTPS).`,
      });
      onRefreshHealth();
      await loadJobs();
    } catch (err) {
      setFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Falha ao executar busca.',
      });
    } finally {
      setRunningDiscovery(false);
    }
  };

  const handleToggleScheduler = async (nextEnabled: boolean) => {
    try {
      const updated = await api.updateScheduler({
        enabled: nextEnabled,
        interval_minutes: schedInterval,
      });
      setScheduler(updated);
      onRefreshHealth();
      setFeedback({
        type: 'ok',
        text: updated.enabled
          ? `Agendador APScheduler ativo a cada ${updated.interval_minutes} min.`
          : 'Agendador APScheduler pausado.',
      });
    } catch (err) {
      setFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Erro ao atualizar agendador.',
      });
    }
  };

  const handleEnqueueKanban = async (jobId: string) => {
    try {
      await api.enqueueApplication({
        opportunity_id: jobId,
        stage: 'candidatura_a_preparar',
      });
      setFeedback({ type: 'ok', text: 'Vaga adicionada à fila do Kanban (Candidatura a Preparar).' });
      onRefreshHealth();
      if (selectedJobId === jobId) {
        const refreshed = await api.getJobDetail(jobId);
        setJobDetail(refreshed);
      }
    } catch (err) {
      setFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Falha ao enviar vaga ao Kanban.',
      });
    }
  };

  const handleSubmitImport = async (e: React.FormEvent) => {
    e.preventDefault();
    setImporting(true);
    try {
      const res = await api.importAssistedJob({
        ...importForm,
        url: importForm.url.trim(),
        title: importForm.title?.trim() || undefined,
        company: importForm.company?.trim() || undefined,
        location: importForm.location?.trim() || undefined,
        contract_hint: importForm.contract_hint?.trim() || undefined,
      });
      setShowImportModal(false);
      setImportForm({
        url: '',
        title: '',
        company: '',
        location: '',
        description: '',
        workplace_mode: null,
        contract_hint: '',
        requires_login: false,
      });
      setSelectedJobId(res.opportunity_id);
      onRefreshHealth();
      await loadJobs();
      setFeedback({
        type: 'ok',
        text: `Vaga importada e triada (${res.triage.classification} / Nível ${res.triage.service_tier ?? 2}).`,
      });
    } catch (err) {
      setFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Erro ao importar vaga.',
      });
    } finally {
      setImporting(false);
    }
  };

  const analysis = jobDetail?.analysis ?? {};
  const matchedSkills = Array.isArray(analysis.matched_skills) ? analysis.matched_skills : [];
  const prepGaps = Array.isArray(analysis.preparable_gaps) ? analysis.preparable_gaps : [];
  const omissions = Array.isArray(analysis.operational_omissions)
    ? analysis.operational_omissions
    : [];
  const incompatibilities = Array.isArray(analysis.hard_incompatibilities)
    ? analysis.hard_incompatibilities
    : [];
  const shortPrepPlans = Array.isArray(analysis.short_prep_plans) ? analysis.short_prep_plans : [];
  const reqStudyPlans = (jobDetail?.requirements ?? []).filter(
    (r) => !r.can_appear_on_resume && r.study_plan
  );

  return (
    <div className={styles.container}>
      {/* KPI Summary */}
      <div className={styles.kpiGrid}>
        <div className={`card ${styles.kpiCard}`}>
          <span className={styles.kpiLabel}>Vagas Avaliadas</span>
          <span className={styles.kpiValue}>{health?.counts.opportunities ?? totalJobs}</span>
          <span className={styles.kpiSub}>Deduplicadas por URL canônica + SHA-256</span>
        </div>
        <div className={`card ${styles.kpiCard}`}>
          <span className={styles.kpiLabel}>Currículos PDF Gerados</span>
          <span className={styles.kpiValue}>{health?.counts.resume_versions ?? 0}</span>
          <span className={styles.kpiSub}>Auditados com validação ATS pypdf</span>
        </div>
        <div className={`card ${styles.kpiCard}`}>
          <span className={styles.kpiLabel}>Candidaturas no Kanban</span>
          <span className={styles.kpiValue}>{health?.counts.tracked_applications ?? 0}</span>
          <span className={styles.kpiSub}>Rastreamento sem submissão cega</span>
        </div>
        <div className={`card ${styles.kpiCard}`}>
          <span className={styles.kpiLabel}>Ciclos de Descoberta</span>
          <span className={styles.kpiValue}>{health?.counts.discovery_runs ?? 0}</span>
          <span className={styles.kpiSub}>
            SQLite: {(health?.sqlite_journal_mode ?? 'wal').toUpperCase()}
          </span>
        </div>
      </div>

      {/* Discovery & APScheduler Controls */}
      <div className={`card ${styles.toolbarCard}`}>
        <div className={styles.toolbarActions}>
          <button
            type="button"
            className="btn btnPrimary"
            disabled={runningDiscovery}
            onClick={handleRunDiscovery}
          >
            {runningDiscovery ? 'Buscando nas fontes públicas...' : 'Buscar Vagas Agora (Zero IA)'}
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => setShowImportModal(true)}
          >
            + Importar Vaga Manual (Nível 2/3)
          </button>
        </div>

        <div className={styles.schedulerBox}>
          <span className="fieldLabel">APScheduler:</span>
          <span className={`badge ${scheduler?.enabled ? 'badgeOk' : 'badgeNeutral'}`}>
            {scheduler?.enabled ? 'Ativo' : 'Pausado'}
          </span>
          <input
            type="number"
            min={15}
            max={1440}
            className={`input ${styles.inlineInput}`}
            value={schedInterval}
            onChange={(e) => setSchedInterval(Number(e.target.value) || 60)}
            title="Intervalo em minutos"
          />
          <span className={styles.jobMeta}>min</span>
          <button
            type="button"
            className="btn btnSmall"
            onClick={() => handleToggleScheduler(!scheduler?.enabled)}
          >
            {scheduler?.enabled ? 'Pausar' : 'Ativar Agendamento'}
          </button>
        </div>
      </div>

      {feedback && (
        <div
          className={`bannerAlert ${feedback.type === 'ok' ? 'badgeOk' : 'badgeDanger'}`}
          role="status"
        >
          <span>{feedback.text}</span>
          <button type="button" className="btn btnSmall" onClick={() => setFeedback(null)}>
            Fechar
          </button>
        </div>
      )}

      {/* Filters */}
      <div className={`card ${styles.filtersCard}`}>
        <div className="fieldGroup">
          <label className="fieldLabel" htmlFor="jobs-search">
            Busca por Cargo, Empresa ou Local
          </label>
          <input
            id="jobs-search"
            className="input"
            placeholder="Ex.: Suporte, Python, Analista, Remoto..."
            value={filterSearch}
            onChange={(e) => setFilterSearch(e.target.value)}
          />
        </div>

        <div className="fieldGroup">
          <label className="fieldLabel" htmlFor="jobs-area">
            Área Profissional
          </label>
          <select
            id="jobs-area"
            className="select"
            value={filterArea}
            onChange={(e) => setFilterArea(e.target.value)}
          >
            <option value="">Todas as áreas</option>
            {Object.values(areaPresets).map((preset) => (
              <option key={preset.area_id} value={preset.area_id}>
                {preset.display_name} ({preset.area_id})
              </option>
            ))}
          </select>
        </div>

        <div className="fieldGroup">
          <label className="fieldLabel" htmlFor="jobs-compat">
            Compatibilidade (Eixo 1)
          </label>
          <select
            id="jobs-compat"
            className="select"
            value={filterCompatibility}
            onChange={(e) => setFilterCompatibility(e.target.value)}
          >
            <option value="">Todas</option>
            <option value="compativel">Compatível</option>
            <option value="compativel_com_preparacao">Compatível c/ Preparação</option>
            <option value="insuficiente_para_avaliar">Insuficiente p/ Avaliar</option>
            <option value="incompativel_por_impedimento_concreto">Incompatível</option>
          </select>
        </div>

        <div className="fieldGroup">
          <label className="fieldLabel" htmlFor="jobs-tier">
            Nível de Automação (Eixo 3)
          </label>
          <select
            id="jobs-tier"
            className="select"
            value={filterTier}
            onChange={(e) => setFilterTier(e.target.value)}
          >
            <option value="">Todos os níveis (1/2/3)</option>
            <option value="1">Nível 1 — Descoberta &amp; Canal Direto</option>
            <option value="2">Nível 2 — Preparação Local Assistida</option>
            <option value="3">Nível 3 — Portal Fechado / Login</option>
          </select>
        </div>

        <button
          type="button"
          className="btn"
          onClick={() => {
            setFilterSearch('');
            setFilterArea('');
            setFilterCompatibility('');
            setFilterTier('');
          }}
        >
          Limpar
        </button>
      </div>

      {/* Jobs Inbox + Triage Inspector */}
      <div className={styles.inboxSplit}>
        <div className={`card ${styles.jobListPanel}`}>
          <div className={styles.listHeader}>
            <span>Caixa de Entrada Triada ({totalJobs} registros)</span>
            {loadingJobs && <span>Atualizando...</span>}
          </div>

          {jobs.length === 0 && !loadingJobs ? (
            <div className={styles.emptyState}>
              Nenhuma vaga encontrada para os filtros selecionados. Use &ldquo;Importar Vaga
              Manual&rdquo; ou ajuste os filtros.
            </div>
          ) : (
            jobs.map((job) => {
              const cBadge = COMPATIBILITY_LABELS[job.professional_compatibility] ?? {
                label: job.professional_compatibility,
                cls: 'badgeNeutral',
              };
              const oBadge = OPERATIONAL_LABELS[job.operational_status] ?? {
                label: job.operational_status,
                cls: 'badgeNeutral',
              };
              const tBadge = TIER_LABELS[job.service_tier] ?? {
                label: `Nível ${job.service_tier}`,
                cls: 'badgeNeutral',
              };

              return (
                <div
                  key={job.id}
                  className={`${styles.jobRow} ${
                    selectedJobId === job.id ? styles.jobRowSelected : ''
                  }`}
                  onClick={() => setSelectedJobId(job.id)}
                >
                  <div className={styles.jobRowTop}>
                    <div>
                      <div className={styles.jobTitle}>{job.title}</div>
                      <div className={styles.jobMeta}>
                        {job.company || 'Empresa não informada'} •{' '}
                        {job.location || 'Local omitido'}
                        {job.workplace_mode ? ` (${job.workplace_mode})` : ''}
                      </div>
                    </div>
                    <span className="badge badgeNeutral">Prio {job.practical_priority}</span>
                  </div>

                  <div className={styles.axisBadgeRow}>
                    <span className={`badge ${cBadge.cls}`}>{cBadge.label}</span>
                    <span className={`badge ${oBadge.cls}`}>{oBadge.label}</span>
                    <span className={`badge ${tBadge.cls}`}>{tBadge.label}</span>
                    {job.area_id && <span className="badge badgeNeutral">Área: {job.area_id}</span>}
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Right Detail Pane */}
        <div className={`card ${styles.detailPanel}`}>
          {loadingDetail ? (
            <div className={styles.emptyState}>Carregando diagnóstico determinístico...</div>
          ) : !jobDetail ? (
            <div className={styles.emptyState}>
              Selecione uma vaga à esquerda para inspecionar os 3 eixos de triagem, skills atendidas
              e planos de estudo internos.
            </div>
          ) : (
            <>
              <div className={styles.detailHeader}>
                <div className={styles.jobRowTop}>
                  <h2>{jobDetail.opportunity.title}</h2>
                  <span className="badge badgeInfo">{jobDetail.opportunity.classification}</span>
                </div>
                <div className={styles.jobMeta}>
                  <strong>{jobDetail.opportunity.company || 'Empresa omitida'}</strong> •{' '}
                  {jobDetail.opportunity.location || 'Localização não especificada'} • Estágio:{' '}
                  <code>{jobDetail.opportunity.application_stage}</code>
                </div>
                <div className={styles.detailActions}>
                  <button
                    type="button"
                    className="btn btnPrimary"
                    onClick={() =>
                      onOpenInStudio(jobDetail.opportunity, jobDetail.opportunity.area_id)
                    }
                  >
                    Abrir no Estúdio de Currículo A4
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => handleEnqueueKanban(jobDetail.opportunity.id)}
                  >
                    Enviar p/ Kanban
                  </button>
                  <a
                    href={jobDetail.opportunity.original_url}
                    target="_blank"
                    rel="noreferrer"
                    className="btn"
                  >
                    Abrir Link Original ↗
                  </a>
                </div>
              </div>

              {/* 3 Independent Axes */}
              <div className={styles.axesGrid}>
                <div className={styles.axisBox}>
                  <span className={styles.axisTitle}>Eixo 1 • Compatibilidade</span>
                  <strong>{jobDetail.opportunity.professional_compatibility}</strong>
                  <span className={styles.jobMeta}>
                    Faixa: {jobDetail.opportunity.career_tier || 'n/d'} | Score:{' '}
                    {jobDetail.opportunity.relevance_score}
                  </span>
                </div>
                <div className={styles.axisBox}>
                  <span className={styles.axisTitle}>Eixo 2 • Operacional / Omissões</span>
                  <strong>{jobDetail.opportunity.operational_status}</strong>
                  <span className={styles.jobMeta}>
                    Contrato: {jobDetail.opportunity.contract_type || 'omitido'} | Modalidade:{' '}
                    {jobDetail.opportunity.workplace_mode || 'omitida'}
                  </span>
                </div>
                <div className={styles.axisBox}>
                  <span className={styles.axisTitle}>Eixo 3 • Automação Ética</span>
                  <strong>Nível {jobDetail.opportunity.service_tier}</strong>
                  <span className={styles.jobMeta}>
                    Método {jobDetail.opportunity.application_method} • Esforço{' '}
                    {jobDetail.opportunity.application_effort}
                  </span>
                </div>
              </div>

              {incompatibilities.length > 0 && (
                <div className={styles.sectionBlock}>
                  <span className={styles.sectionHeading}>Impedimentos Concretos Detectados</span>
                  <div className={styles.tagCloud}>
                    {incompatibilities.map((item, idx) => (
                      <span key={idx} className="badge badgeDanger">
                        {String(item)}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {omissions.length > 0 && (
                <div className={styles.sectionBlock}>
                  <span className={styles.sectionHeading}>
                    Omissões Operacionais (Não bloqueiam preparação local do PDF)
                  </span>
                  <div className={styles.tagCloud}>
                    {omissions.map((om, idx) => (
                      <span key={idx} className="badge badgeWarn">
                        {String(om)}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              <div className={styles.sectionBlock}>
                <span className={styles.sectionHeading}>
                  Skills Confirmadas / Base Acadêmica Atendidas ({matchedSkills.length})
                </span>
                <div className={styles.tagCloud}>
                  {matchedSkills.length === 0 ? (
                    <span className={styles.jobMeta}>Nenhuma skill mapeada diretamente.</span>
                  ) : (
                    matchedSkills.map((sk) => (
                      <span key={String(sk)} className="badge badgeOk">
                        ✓ {String(sk)}
                      </span>
                    ))
                  )}
                </div>
              </div>

              {(prepGaps.length > 0 || shortPrepPlans.length > 0 || reqStudyPlans.length > 0) && (
                <div className={styles.sectionBlock}>
                  <span className={styles.sectionHeading}>
                    Lacunas Preparáveis (`preparable_gap`) &amp; Plano de Estudo Interno (Bloqueadas
                    no PDF)
                  </span>
                  <div className={styles.tagCloud}>
                    {prepGaps.map((gap) => (
                      <span key={String(gap)} className="badge badgeWarn">
                        ⚡ {String(gap)}
                      </span>
                    ))}
                  </div>

                  {reqStudyPlans.map((req) => (
                    <div key={req.skill_id} className={styles.studyPlanCard}>
                      <span className={styles.studyPlanTerm}>
                        Plano de Estudo — {req.skill_name} ({req.veracity_state})
                      </span>
                      <span className={styles.studyPlanBody}>{req.study_plan}</span>
                    </div>
                  ))}

                  {shortPrepPlans.map((plan, idx) => (
                    <div key={idx} className={styles.studyPlanCard}>
                      <span className={styles.studyPlanTerm}>
                        Roteiro Rápido Pré-Entrevista — {plan.term}
                      </span>
                      <span className={styles.studyPlanBody}>{plan.study_plan}</span>
                    </div>
                  ))}
                </div>
              )}

              {jobDetail.opportunity.description_text && (
                <div className={styles.sectionBlock}>
                  <span className={styles.sectionHeading}>Descrição Armazenada da Vaga</span>
                  <div className={styles.descriptionBox}>
                    {jobDetail.opportunity.description_text}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Modal: Assisted Manual Import */}
      {showImportModal && (
        <div className="modalBackdrop" onClick={() => setShowImportModal(false)}>
          <form
            className="modalDialog"
            onClick={(e) => e.stopPropagation()}
            onSubmit={handleSubmitImport}
          >
            <div className="modalHeader">
              <h3>Importação Manual Assistida (Nível 2 e Nível 3)</h3>
              <button
                type="button"
                className="btn btnSmall"
                onClick={() => setShowImportModal(false)}
              >
                ✕
              </button>
            </div>

            <div className="fieldGroup">
              <label className="fieldLabel">URL da Vaga (HTTPS obrigatório) *</label>
              <input
                required
                type="url"
                className="input"
                placeholder="https://portal.exemplo.com/vagas/123"
                value={importForm.url}
                onChange={(e) => setImportForm({ ...importForm, url: e.target.value })}
              />
            </div>

            <div className={styles.formGrid2}>
              <div className="fieldGroup">
                <label className="fieldLabel">Cargo / Título</label>
                <input
                  className="input"
                  placeholder="Ex.: Analista de Suporte Júnior"
                  value={importForm.title ?? ''}
                  onChange={(e) => setImportForm({ ...importForm, title: e.target.value })}
                />
              </div>
              <div className="fieldGroup">
                <label className="fieldLabel">Empresa</label>
                <input
                  className="input"
                  placeholder="Nome da empresa"
                  value={importForm.company ?? ''}
                  onChange={(e) => setImportForm({ ...importForm, company: e.target.value })}
                />
              </div>
            </div>

            <div className={styles.formGrid2}>
              <div className="fieldGroup">
                <label className="fieldLabel">Localização</label>
                <input
                  className="input"
                  placeholder="Cidade / UF ou Remoto"
                  value={importForm.location ?? ''}
                  onChange={(e) => setImportForm({ ...importForm, location: e.target.value })}
                />
              </div>
              <div className="fieldGroup">
                <label className="fieldLabel">Modalidade</label>
                <select
                  className="select"
                  value={importForm.workplace_mode ?? ''}
                  onChange={(e) =>
                    setImportForm({
                      ...importForm,
                      workplace_mode: (e.target.value || null) as
                        | 'presencial'
                        | 'hibrido'
                        | 'remoto'
                        | null,
                    })
                  }
                >
                  <option value="">Inferir pelo texto</option>
                  <option value="presencial">Presencial</option>
                  <option value="hibrido">Híbrido</option>
                  <option value="remoto">Remoto</option>
                </select>
              </div>
            </div>

            <div className="fieldGroup">
              <label className="fieldLabel">Descrição Completa da Vaga *</label>
              <textarea
                required
                rows={6}
                className="textarea"
                placeholder="Cole aqui os requisitos, responsabilidades, horário e regime de contratação..."
                value={importForm.description}
                onChange={(e) => setImportForm({ ...importForm, description: e.target.value })}
              />
            </div>

            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
              <input
                type="checkbox"
                checked={Boolean(importForm.requires_login)}
                onChange={(e) =>
                  setImportForm({ ...importForm, requires_login: e.target.checked })
                }
              />
              Portal exige login/cadastro do candidato (Classificar no Nível 3)
            </label>

            <div className="modalActions">
              <button
                type="button"
                className="btn"
                onClick={() => setShowImportModal(false)}
              >
                Cancelar
              </button>
              <button type="submit" className="btn btnPrimary" disabled={importing}>
                {importing ? 'Triando e Salvando...' : 'Triar e Salvar Vaga'}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
};
