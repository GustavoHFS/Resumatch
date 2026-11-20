import React, { useCallback, useEffect, useState } from 'react';
import { api, SERVER_DISCONNECTED_MESSAGE } from '../api/client';
import {
  IconClose,
  IconExternal,
  IconPlus,
  IconSearch,
  IconSparkles,
} from '../components/Icons';
import type {
  AssistedImportPayload,
  JobDetailResponse,
  JobListItem,
  SearchCriteria,
} from '../types/api';
import styles from './JobsPage.module.css';

interface Props {
  onAdaptJob: (job: JobListItem, descriptionText: string) => Promise<void>;
}

const RECENCY_OPTIONS = [
  { value: '14d', label: 'Últimos 14 dias' },
  { value: '7d', label: 'Últimos 7 dias' },
  { value: '24h', label: 'Últimas 24h' },
  { value: 'all', label: 'Todo o histórico' },
];

const AREA_OPTIONS = [
  { value: '', label: 'Todas as áreas' },
  { value: 'ti_geral', label: 'TI Geral / Sistemas' },
  { value: 'suporte', label: 'Suporte & Infraestrutura' },
  { value: 'dev', label: 'Desenvolvimento' },
  { value: 'dados', label: 'Dados & BI' },
  { value: 'cybersec', label: 'Redes & Segurança' },
];

function buildJobMeta(job: JobListItem): string {
  const parts: string[] = [job.company || 'Empresa não informada'];
  const location = (job.location || '').trim();
  const normalizedLocation = location
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase();
  if (location && normalizedLocation !== 'remoto' && normalizedLocation !== 'remote') {
    parts.push(location);
  }
  const workplace = job.labels?.workplace_label;
  if (job.workplace_mode && workplace && workplace !== 'Modalidade não informada') {
    parts.push(workplace);
  }
  if (job.source_label) {
    parts.push(`Via ${job.source_label}`);
  }
  return parts.join(' · ');
}

export const JobsPage: React.FC<Props> = ({ onAdaptJob }) => {
  const [jobs, setJobs] = useState<JobListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchingSources, setSearchingSources] = useState(false);

  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [recencyFilter, setRecencyFilter] = useState('14d');
  const [areaFilter, setAreaFilter] = useState('');
  const [criteria, setCriteria] = useState<SearchCriteria | null>(null);
  const [remoteActive, setRemoteActive] = useState(true);
  const [savingRemote, setSavingRemote] = useState(false);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<JobDetailResponse | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    const clear = () => setNotice((current) => current === SERVER_DISCONNECTED_MESSAGE ? null : current);
    window.addEventListener('resumatch:api-connected', clear);
    return () => window.removeEventListener('resumatch:api-connected', clear);
  }, []);
  const [showPasteModal, setShowPasteModal] = useState(false);
  const [savingPaste, setSavingPaste] = useState(false);
  const [pasteForm, setPasteForm] = useState<AssistedImportPayload>({
    url: '',
    title: '',
    company: '',
    location: '',
    description: '',
    workplace_mode: null,
  });

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search), 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  const loadJobs = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const res = await api.getJobs({
        search: debouncedSearch || undefined,
        recency: recencyFilter,
        area_id: areaFilter || undefined,
        include_remote: remoteActive ? undefined : false,
        limit: 100,
      }, signal);
      if (signal?.aborted) return;
      setJobs(res.items);
      setSelectedId((current) => res.items.some((j) => j.id === current)
        ? current : res.items[0]?.id ?? null);
    } catch (err) {
      if (!signal?.aborted) setNotice(err instanceof Error ? err.message : 'Não foi possível carregar as vagas.');
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [debouncedSearch, recencyFilter, areaFilter, remoteActive]);

  useEffect(() => {
    const controller = new AbortController();
    void loadJobs(controller.signal);
    return () => controller.abort();
  }, [loadJobs]);

  useEffect(() => {
    api
      .getCriteria()
      .then((loaded) => {
        setCriteria(loaded);
        setRemoteActive(loaded.include_remote !== false);
      })
      .catch(() => {});
  }, []);

  const handleToggleRemote = async () => {
    const next = !remoteActive;
    setRemoteActive(next);
    setSavingRemote(true);
    try {
      let current = criteria;
      if (!current) {
        current = await api.getCriteria();
      }
      const updated: SearchCriteria = { ...current, include_remote: next };
      await api.saveCriteria(updated);
      setCriteria(updated);
      setNotice(
        next
          ? 'Vagas remotas ativadas nas próximas buscas.'
          : 'Vagas remotas ocultadas nas próximas buscas.'
      );
    } catch (err) {
      setRemoteActive(!next);
      setNotice(err instanceof Error ? err.message : 'Não foi possível salvar a preferência.');
    } finally {
      setSavingRemote(false);
    }
  };

  const visibleJobs = remoteActive
    ? jobs
    : jobs.filter((job) => job.workplace_mode !== 'remoto');

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setLoadingDetail(true);
    api
      .getJobDetail(selectedId)
      .then((res) => {
        if (!cancelled) setDetail(res);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      })
      .finally(() => {
        if (!cancelled) setLoadingDetail(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const handleRunDiscovery = async () => {
    setSearchingSources(true);
    setNotice(null);
    try {
      const res = await api.runDiscoveryNow(80);
      setNotice(
        res.new_count > 0
          ? `${res.new_count} nova(s) vaga(s) recente(s) encontrada(s).`
          : `Busca concluída (${res.unique_count} vagas recentes verificadas, nenhuma nova).`
      );
      await loadJobs();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Erro ao buscar vagas.');
    } finally {
      setSearchingSources(false);
    }
  };

  const handleSaveToApplications = async (opportunityId: string) => {
    try {
      await api.enqueueApplication(opportunityId);
      setNotice('Vaga adicionada em Candidaturas.');
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Erro ao salvar candidatura.');
    }
  };

  const handlePasteSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const adapt = (e.nativeEvent as SubmitEvent).submitter?.getAttribute('value') === 'adapt';
    setSavingPaste(true);
    try {
      const normalizedUrl = pasteForm.url.trim();
      const textLines = pasteForm.description.split('\n').map((line) => line.trim()).filter(Boolean);
      const inferredTitle = textLines.find((line) => /^(?:cargo|vaga|título)\s*:/i.test(line))
        ?.replace(/^[^:]+:\s*/, '') || textLines[0]?.slice(0, 180) || 'Vaga colada';
      const inferredCompany = textLines.find((line) => /^empresa\s*:/i.test(line))
        ?.replace(/^[^:]+:\s*/, '');
      const res = await api.importAssistedJob({
        ...pasteForm,
        url: normalizedUrl.startsWith('http://')
          ? normalizedUrl.replace(/^http:\/\//i, 'https://')
          : normalizedUrl,
        title: pasteForm.title?.trim() || inferredTitle,
        company: pasteForm.company?.trim() || inferredCompany,
        location: pasteForm.location?.trim() || undefined,
      });
      setShowPasteModal(false);
      setPasteForm({
        url: '',
        title: '',
        company: '',
        location: '',
        description: '',
        workplace_mode: null,
      });
      setSelectedId(res.opportunity_id);
      await loadJobs();
      if (adapt) {
        const selected = await api.getJobDetail(res.opportunity_id);
        await onAdaptJob(selected.opportunity, selected.opportunity.description_text || pasteForm.description);
      }
      setNotice('Vaga adicionada à lista.');
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Verifique os dados da vaga colada.');
    } finally {
      setSavingPaste(false);
    }
  };

  const matchedSkills = detail?.analysis?.matched_skills ?? [];

  return (
    <div className={styles.page}>
      {/* Compact Editorial Toolbar */}
      <div className={styles.toolbar}>
        <div className={styles.searchWrap}>
          <span className={styles.searchIcon}>
            <IconSearch size={15} />
          </span>
          <input
            className={`input ${styles.searchInput}`}
            placeholder="Buscar por cargo, empresa ou cidade..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        <select
          className="select"
          value={recencyFilter}
          onChange={(e) => setRecencyFilter(e.target.value)}
          aria-label="Filtrar por data de publicação"
        >
          {RECENCY_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>

        <select
          className="select"
          value={areaFilter}
          onChange={(e) => setAreaFilter(e.target.value)}
          aria-label="Filtrar por área"
        >
          {AREA_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>

        <button
          type="button"
          className="btnSubtle"
          aria-pressed={remoteActive}
          disabled={savingRemote}
          onClick={() => void handleToggleRemote()}
          title="Incluir ou ocultar vagas remotas na lista e nas próximas buscas"
        >
          <span>{remoteActive ? 'Vagas Remotas: Ativas' : 'Vagas Remotas: Ocultas'}</span>
        </button>

        <button
          type="button"
          className="btnSubtle"
          disabled={searchingSources}
          onClick={handleRunDiscovery}
        >
          <IconSearch size={15} />
          <span>{searchingSources ? 'Buscando...' : 'Buscar Vagas'}</span>
        </button>

        <button
          type="button"
          className="btnGhost"
          onClick={() => setShowPasteModal(true)}
        >
          <IconPlus size={15} />
          <span>Colar Vaga</span>
        </button>
      </div>

      {notice && (
        <div className="noticeBar" role="status">
          <span>{notice}</span>
          <button
            type="button"
            className="iconBtn"
            onClick={() => setNotice(null)}
            aria-label="Fechar aviso"
          >
            <IconClose size={15} />
          </button>
        </div>
      )}

      {/* Editorial List + Clean Side Panel */}
      <div className={styles.split}>
        <div className={`panel ${styles.listPanel}`}>
          {loading && visibleJobs.length === 0 ? (
            <div className={styles.empty}>Carregando vagas...</div>
          ) : visibleJobs.length === 0 ? (
            <div className={styles.empty}>
              <p>Nenhuma vaga listada para este filtro.</p>
              <button type="button" className="btnSubtle" onClick={() => {
                setSearch(''); setAreaFilter(''); setRecencyFilter('14d');
              }}>Limpar filtros</button>
              <button type="button" className="btnGhost" onClick={() => void handleRunDiscovery()}>Buscar novas vagas</button>
            </div>
          ) : (
            visibleJobs.map((job) => (
              <button
                key={job.id}
                type="button"
                className={`${styles.jobItem} ${
                  selectedId === job.id ? styles.jobItemActive : ''
                }`}
                onClick={() => setSelectedId(job.id)}
              >
                <div className={styles.jobRowTop}>
                  <span className={styles.jobTitle}>{job.title}</span>
                  <span className={styles.publishedBadge}>{job.published_ago}</span>
                </div>
                <div className={styles.jobSub}>{buildJobMeta(job)}</div>
              </button>
            ))
          )}
        </div>

        <div className={`panel ${styles.detailPanel}`}>
          {loadingDetail ? (
            <div className={styles.empty}>Carregando detalhes...</div>
          ) : !detail ? (
            <div className={styles.empty}>Selecione uma vaga para ler os detalhes.</div>
          ) : (
            <>
              <div className={styles.detailHeader}>
                <h2 className={styles.detailTitle}>{detail.opportunity.title}</h2>
                <div className={styles.detailMeta}>
                  {detail.opportunity.published_ago && (
                    <span className={styles.publishedBadge}>{detail.opportunity.published_ago}</span>
                  )}
                  <span>{buildJobMeta(detail.opportunity)}</span>
                </div>

                <div className={styles.ctaRow}>
                  <button
                    type="button"
                    className={`btnGhost ${styles.primaryButton}`}
                    onClick={() =>
                      void onAdaptJob(detail.opportunity, detail.opportunity.description_text || '')
                        .catch((err) => setNotice(err instanceof Error ? err.message : 'Erro ao salvar candidatura.'))
                    }
                  >
                    <IconSparkles size={15} />
                    <span>Adaptar no Estúdio</span>
                  </button>

                  <button
                    type="button"
                    className="btnSubtle"
                    onClick={() => void handleSaveToApplications(detail.opportunity.id)}
                  >
                    Salvar em Candidaturas
                  </button>

                  {detail.opportunity.original_url && (
                    <a
                      href={detail.opportunity.original_url}
                      target="_blank"
                      rel="noreferrer"
                      className="btnSubtle"
                    >
                      <span>Link original</span>
                      <IconExternal size={14} />
                    </a>
                  )}
                </div>
              </div>

              {matchedSkills.length > 0 && (
                <div className={styles.matchedBlock}>
                  <span className="label">Competências identificadas no seu perfil</span>
                  <div className={styles.tagsWrap}>
                    {matchedSkills.map((sk) => (
                      <span key={sk} className="tag">
                        {sk}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {detail.opportunity.description_text ? (
                <div className={styles.descriptionText}>{detail.opportunity.description_text}</div>
              ) : (
                <div className={styles.empty}>Sem texto completo salvo para este anúncio.</div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Paste Job Modal */}
      {showPasteModal && (
        <div className="overlay" onClick={() => setShowPasteModal(false)}>
          <form
            className="modal"
            onClick={(e) => e.stopPropagation()}
            onSubmit={handlePasteSubmit}
          >
            <div className="modalHeader">
              <h2 className="modalTitle">Colar Vaga</h2>
              <button
                type="button"
                className="iconBtn"
                onClick={() => setShowPasteModal(false)}
                aria-label="Fechar"
              >
                <IconClose size={16} />
              </button>
            </div>

            <div className={styles.grid2}>
              <div className="field">
                <label className="label">Cargo</label>
                <input
                  className="input"
                  placeholder="Ex.: Desenvolvedor Júnior"
                  value={pasteForm.title ?? ''}
                  onChange={(e) => setPasteForm({ ...pasteForm, title: e.target.value })}
                />
              </div>
              <div className="field">
                <label className="label">Empresa</label>
                <input
                  className="input"
                  placeholder="Nome da empresa"
                  value={pasteForm.company ?? ''}
                  onChange={(e) => setPasteForm({ ...pasteForm, company: e.target.value })}
                />
              </div>
            </div>

            <div className={styles.grid2}>
              <div className="field">
                <label className="label">Localização</label>
                <input
                  className="input"
                  placeholder="Ex.: São Paulo, SP ou Remoto"
                  value={pasteForm.location ?? ''}
                  onChange={(e) => setPasteForm({ ...pasteForm, location: e.target.value })}
                />
              </div>
              <div className="field">
                <label className="label">Link da vaga (https://)</label>
                <input
                  type="url"
                  className="input"
                  placeholder="Opcional: https://..."
                  value={pasteForm.url}
                  onChange={(e) => setPasteForm({ ...pasteForm, url: e.target.value })}
                />
              </div>
            </div>

            <div className="field">
              <label className="label">Descrição da vaga</label>
              <textarea
                required
                rows={6}
                className="textarea"
                placeholder="Cole aqui as atividades e os requisitos da vaga..."
                value={pasteForm.description}
                onChange={(e) => setPasteForm({ ...pasteForm, description: e.target.value })}
              />
            </div>

            <div className="modalFooter">
              <button
                type="button"
                className="btnSubtle"
                onClick={() => setShowPasteModal(false)}
              >
                Cancelar
              </button>
              <button type="submit" value="add" className="btnSubtle" disabled={savingPaste}>
                {savingPaste ? 'Salvando...' : 'Adicionar Vaga'}
              </button>
              <button type="submit" value="adapt" className={`btnGhost ${styles.primaryButton}`} disabled={savingPaste}>
                Adicionar e Adaptar Currículo
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
};
