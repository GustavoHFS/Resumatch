import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import {
  IconClose,
  IconExternal,
  IconPlus,
  IconSearch,
  IconSparkles,
} from '../components/Icons';
import type { AssistedImportPayload, JobDetailResponse, JobListItem } from '../types/api';
import styles from './JobsPage.module.css';

interface Props {
  onAdaptJob: (job: JobListItem, descriptionText: string) => void;
}

const RECENCY_OPTIONS = [
  { value: '14d', label: 'Últimos 14 dias' },
  { value: '7d', label: 'Últimos 7 dias' },
  { value: '24h', label: 'Últimas 24h' },
  { value: 'all', label: 'Todo o histórico' },
];

const AREA_OPTIONS = [
  { value: '', label: 'Todas as áreas' },
  { value: 'dev', label: 'Desenvolvimento' },
  { value: 'suporte', label: 'Suporte e Redes' },
  { value: 'dados', label: 'Dados e BI' },
  { value: 'cybersec', label: 'Segurança da Informação' },
  { value: 'administrativo', label: 'Administrativo' },
];

const COMPATIBILITY_OPTIONS = [
  { value: '', label: 'Todos os status' },
  { value: 'compativel', label: 'Compatíveis' },
  { value: 'compativel_com_preparacao', label: 'Compatíveis com estudo rápido' },
  { value: 'insuficiente_para_avaliar', label: 'Descrição incompleta' },
  { value: 'incompativel_por_impedimento_concreto', label: 'Fora dos critérios' },
];

export const JobsPage: React.FC<Props> = ({ onAdaptJob }) => {
  const [jobs, setJobs] = useState<JobListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchingSources, setSearchingSources] = useState(false);

  const [search, setSearch] = useState('');
  const [recencyFilter, setRecencyFilter] = useState('14d');
  const [areaFilter, setAreaFilter] = useState('');
  const [compatFilter, setCompatFilter] = useState('');

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<JobDetailResponse | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const [notice, setNotice] = useState<string | null>(null);
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

  const loadJobs = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.getJobs({
        search: search || undefined,
        recency: recencyFilter,
        area_id: areaFilter || undefined,
        compatibility: compatFilter || undefined,
        limit: 100,
      });
      setJobs(res.items);
      if (res.items.length > 0 && !res.items.some((j) => j.id === selectedId)) {
        setSelectedId(res.items[0].id);
      } else if (res.items.length === 0) {
        setSelectedId(null);
      }
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Não foi possível carregar as vagas.');
    } finally {
      setLoading(false);
    }
  }, [search, recencyFilter, areaFilter, compatFilter, selectedId]);

  useEffect(() => {
    void loadJobs();
  }, [loadJobs]);

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
    setSavingPaste(true);
    try {
      const normalizedUrl = pasteForm.url.trim() || 'https://vaga-manual.local/anuncio';
      const res = await api.importAssistedJob({
        ...pasteForm,
        url: normalizedUrl.startsWith('http://')
          ? normalizedUrl.replace(/^http:\/\//i, 'https://')
          : normalizedUrl,
        title: pasteForm.title?.trim() || undefined,
        company: pasteForm.company?.trim() || undefined,
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

        <select
          className="select"
          value={compatFilter}
          onChange={(e) => setCompatFilter(e.target.value)}
          aria-label="Filtrar por compatibilidade"
        >
          {COMPATIBILITY_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>

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
          {loading && jobs.length === 0 ? (
            <div className={styles.empty}>Carregando vagas...</div>
          ) : jobs.length === 0 ? (
            <div className={styles.empty}>
              Nenhuma vaga listada para este filtro. Use &ldquo;Buscar Vagas&rdquo; ou &ldquo;Colar
              Vaga&rdquo;.
            </div>
          ) : (
            jobs.map((job) => (
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
                <div className={styles.jobSub}>
                  {job.company || 'Empresa não informada'}
                  {job.location ? ` · ${job.location}` : ''}
                  {job.workplace_mode ? ` · ${job.labels.workplace_label}` : ''}
                  {` · ${job.labels.compatibility_label}`}
                </div>
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
                  <span>{detail.opportunity.company || 'Empresa não informada'}</span>
                  {detail.opportunity.location && <span>· {detail.opportunity.location}</span>}
                  {detail.opportunity.workplace_mode && (
                    <span>· {detail.opportunity.labels.workplace_label}</span>
                  )}
                  <span>· {detail.opportunity.labels.compatibility_label}</span>
                </div>

                <div className={styles.ctaRow}>
                  <button
                    type="button"
                    className="btnGhost"
                    onClick={() =>
                      onAdaptJob(detail.opportunity, detail.opportunity.description_text || '')
                    }
                  >
                    <IconSparkles size={15} />
                    <span>Adaptar Currículo</span>
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
                  required
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
                  placeholder="Ex.: Recife, PE ou Remoto"
                  value={pasteForm.location ?? ''}
                  onChange={(e) => setPasteForm({ ...pasteForm, location: e.target.value })}
                />
              </div>
              <div className="field">
                <label className="label">Link da vaga (https://)</label>
                <input
                  type="url"
                  required
                  className="input"
                  placeholder="https://..."
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
              <button type="submit" className="btnGhost" disabled={savingPaste}>
                {savingPaste ? 'Salvando...' : 'Adicionar Vaga'}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
};
