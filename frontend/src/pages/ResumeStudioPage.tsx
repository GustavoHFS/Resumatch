import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api, SERVER_DISCONNECTED_MESSAGE } from '../api/client';
import { GoogleConnectCard } from '../components/GoogleConnectCard';
import {
  IconArrowDown,
  IconArrowUp,
  IconChevronDown,
  IconClose,
  IconDownload,
  IconPlus,
  IconSparkles,
  IconTrash,
} from '../components/Icons';
import type { ApplicationCard, JobListItem, ResumeDraft, ResumeFontFamily, SectionKind } from '../types/api';
import styles from './ResumeStudioPage.module.css';

interface Props {
  active: boolean;
  aiConnected: boolean;
  initialJob: JobListItem | null;
  initialJobDescription: string;
  onAiConnected: () => void;
}

const A4_HEIGHT_PX = 1122.5;
const PAGE_MARGIN_TOP_PX = 52.9; // 14mm at 96dpi
const PRINTABLE_CONTENT_PER_PAGE_PX = 1016.7; // 269mm (297mm - 28mm @page margins)

const FONT_OPTIONS: Array<{ value: ResumeFontFamily; label: string }> = [
  { value: 'Libre Franklin', label: 'Libre Franklin (Padrão ATS)' },
  { value: 'Inter', label: 'Inter' },
  { value: 'Arial', label: 'Arial' },
  { value: 'Georgia', label: 'Georgia' },
  { value: 'Times New Roman', label: 'Times New Roman' },
];

const SECTION_NAMES_PT: Record<SectionKind, string> = {
  summary: 'Resumo Profissional',
  experience: 'Experiência Profissional',
  education: 'Formação Acadêmica',
  projects: 'Projetos',
  courses: 'Cursos e Certificações',
  skills: 'Competências Técnicas',
  languages: 'Idiomas',
};

export const ResumeStudioPage: React.FC<Props> = ({
  active,
  aiConnected,
  initialJob,
  initialJobDescription,
  onAiConnected,
}) => {
  const [applications, setApplications] = useState<ApplicationCard[]>([]);
  const [draftOpportunityId, setDraftOpportunityId] = useState<string | null>(null);
  const [selectedOpportunityId, setSelectedOpportunityId] = useState<string>(initialJob?.id ?? '');
  const [showManualBox, setShowManualBox] = useState<boolean>(!initialJob);
  const [manualTitle, setManualTitle] = useState<string>(initialJob?.title ?? '');
  const [manualCompany, setManualCompany] = useState<string>(initialJob?.company ?? '');
  const [manualDescription, setManualDescription] = useState<string>(initialJobDescription);
  const [maxPages, setMaxPages] = useState<number>(1);
  const [fontFamily, setFontFamily] = useState<ResumeFontFamily>('Libre Franklin');

  const [draft, setDraft] = useState<ResumeDraft | null>(null);
  const [previewHtml, setPreviewHtml] = useState<string>('');
  const [openSec, setOpenSec] = useState<Record<string, boolean>>({
    header: false,
    summary: true,
    experience: true,
  });

  const [generating, setGenerating] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    const clear = () => setNotice((current) => current === SERVER_DISCONNECTED_MESSAGE ? null : current);
    window.addEventListener('resumatch:api-connected', clear);
    return () => window.removeEventListener('resumatch:api-connected', clear);
  }, []);
  const [loadingSelectedJob, setLoadingSelectedJob] = useState(false);
  const [skillInputs, setSkillInputs] = useState<Record<string, string>>({});
  const [downloadJob, setDownloadJob] = useState<{ id: string; url: string } | null>(null);

  const [zoom, setZoom] = useState<number>(0.72);
  const [contentFlowPx, setContentFlowPx] = useState<number>(680);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const renderSeqRef = useRef<number>(0);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    api.listApplications().then((res) => {
      if (!cancelled) setApplications(res.items);
    }).catch((err) => {
      if (!cancelled) setNotice(err instanceof Error ? err.message : 'Erro ao carregar candidaturas.');
    });
    return () => { cancelled = true; };
  }, [active, initialJob]);

  useEffect(() => {
    if (initialJob) {
      setDraft(null);
      setDownloadJob(null);
      setSelectedOpportunityId(initialJob.id);
      setManualTitle(initialJob.title);
      setManualCompany(initialJob.company ?? '');
      setManualDescription(initialJobDescription);
      setShowManualBox(false);
    }
  }, [initialJob, initialJobDescription]);

  useEffect(() => {
    if (!selectedOpportunityId) return;
    let cancelled = false;
    setLoadingSelectedJob(true);
    setManualDescription('');
    api.getJobDetail(selectedOpportunityId).then((res) => {
      if (cancelled) return;
      setManualTitle(res.opportunity.title);
      setManualCompany(res.opportunity.company ?? '');
      setManualDescription(res.opportunity.description_text ?? '');
    }).catch((err) => {
      if (!cancelled) setNotice(err instanceof Error ? err.message : 'Não foi possível carregar a vaga.');
    }).finally(() => {
      if (!cancelled) setLoadingSelectedJob(false);
    });
    return () => { cancelled = true; };
  }, [selectedOpportunityId]);

  // Live HTML refresh whenever draft changes (with AbortController + sequence guard against race conditions)
  useEffect(() => {
    if (!draft) {
      setPreviewHtml('');
      return;
    }
    const seq = ++renderSeqRef.current;
    const abortCtrl = new AbortController();
    const timer = window.setTimeout(() => {
      api
        .renderResumeHtml(draft, abortCtrl.signal)
        .then((res) => {
          if (seq === renderSeqRef.current) {
            setPreviewHtml(res.html);
          }
        })
        .catch(() => {});
    }, 200);
    return () => {
      window.clearTimeout(timer);
      abortCtrl.abort();
    };
  }, [draft]);

  const handleGenerate = async () => {
    if (loadingSelectedJob) return;
    setGenerating(true);
    setNotice(null);
    try {
      const res = await api.generateResume({
        opportunity_id: selectedOpportunityId || null,
        job_title: manualTitle || undefined,
        company: manualCompany || null,
        job_description: manualDescription || undefined,
        max_pages: maxPages,
        font_family: fontFamily,
      });
      setDraft(res.draft);
      setDraftOpportunityId(res.job_context.opportunity_id);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Não foi possível gerar o currículo.');
    } finally {
      setGenerating(false);
    }
  };

  const handleExportAndDownload = async () => {
    if (!draft) return;
    setExporting(true);
    setNotice(null);
    try {
      const res = await api.exportResumePdf({
        draft,
        opportunity_id: draftOpportunityId,
        manual_job_description: !draftOpportunityId ? manualDescription : undefined,
        auto_fit: true,
      });
      setDraft(res.draft);
      const downloadUrl = `/api/resumes/versions/${encodeURIComponent(res.version_id)}/pdf`;
      const anchor = document.createElement('a');
      anchor.href = downloadUrl;
      anchor.download = res.output_pdf_filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      if (res.opportunity_id) {
        setSelectedOpportunityId(res.opportunity_id);
        setDownloadJob({ id: res.opportunity_id, url: res.job_url ?? '' });
      }
      setNotice(`PDF "${res.output_pdf_filename}" gerado e baixado.`);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Erro ao exportar o PDF.');
    } finally {
      setExporting(false);
    }
  };

  const copyText = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setNotice('Copiado para a área de transferência.');
    } catch {
      setNotice('Não foi possível copiar o texto.');
    }
  };

  const restoreSection = async (kind: SectionKind) => {
    if (!draft) return;
    try {
      const profile = await api.getProfile();
      const restored: Partial<ResumeDraft> = {};
      if (kind === 'summary') restored.summary = profile.summary;
      if (kind === 'experience') restored.experiences = profile.experiences.map((exp) => ({
        ...exp, highlights: exp.description ? [exp.description] : [],
      }));
      if (kind === 'education') restored.education = profile.education;
      if (kind === 'projects') restored.projects = profile.projects.map((prj) => ({
        ...prj, highlights: [],
      }));
      if (kind === 'courses') restored.courses = profile.courses;
      if (kind === 'skills') restored.skill_groups = [{
        id: 'base_skills', label: 'Competências Técnicas', items: profile.skills,
      }];
      if (kind === 'languages') restored.languages = profile.languages;
      setDraft((current) => current ? { ...current, ...restored } : current);
      setNotice(`${SECTION_NAMES_PT[kind]} restaurado do Currículo Base.`);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Não foi possível restaurar a seção.');
    }
  };

  const toggleOpen = (key: string) => {
    setOpenSec((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const moveSection = (idx: number, delta: -1 | 1) => {
    if (!draft) return;
    const order = [...draft.section_order];
    const target = idx + delta;
    if (target < 0 || target >= order.length) return;
    const [moved] = order.splice(idx, 1);
    order.splice(target, 0, moved);
    setDraft({ ...draft, section_order: order });
  };

  const deleteEntireSection = (kind: SectionKind) => {
    if (!draft) return;
    const nextOrder = draft.section_order.filter((k) => k !== kind);
    const cleared: Partial<ResumeDraft> = { section_order: nextOrder };
    if (kind === 'summary') cleared.summary = '';
    if (kind === 'experience') cleared.experiences = [];
    if (kind === 'education') cleared.education = [];
    if (kind === 'projects') cleared.projects = [];
    if (kind === 'courses') cleared.courses = [];
    if (kind === 'skills') cleared.skill_groups = [];
    if (kind === 'languages') cleared.languages = [];
    setDraft({ ...draft, ...cleared });
  };

  const measureIframe = useCallback(() => {
    const doc = iframeRef.current?.contentDocument;
    if (!doc?.body) return;
    const measureOnce = () => {
      const flowNode = doc.getElementById('resume-flow');
      if (flowNode) {
        const flowH = Math.ceil(flowNode.getBoundingClientRect().height || flowNode.scrollHeight);
        if (flowH > 40) setContentFlowPx(flowH);
      } else {
        const fallbackH = Math.max(100, doc.body.scrollHeight - PAGE_MARGIN_TOP_PX * 2);
        setContentFlowPx(fallbackH);
      }
    };
    measureOnce();
    if ('fonts' in doc && doc.fonts?.ready) {
      doc.fonts.ready.then(measureOnce).catch(() => {});
    }
  }, []);

  const activeMaxPages = draft?.max_pages ?? maxPages;
  const contentBudgetPx = activeMaxPages * PRINTABLE_CONTENT_PER_PAGE_PX;
  const isOverflow = contentFlowPx > contentBudgetPx + 6;
  const estimatedPages = Math.max(1, Math.ceil((contentFlowPx - 6) / PRINTABLE_CONTENT_PER_PAGE_PX));
  const visualSheetPages = Math.max(activeMaxPages, estimatedPages);
  const iframeHeight =
    visualSheetPages === 1
      ? A4_HEIGHT_PX
      : Math.max(visualSheetPages * A4_HEIGHT_PX, contentFlowPx + PAGE_MARGIN_TOP_PX * 2 + 12);
  const limitRuleTopPx = PAGE_MARGIN_TOP_PX + activeMaxPages * PRINTABLE_CONTENT_PER_PAGE_PX;

  return (
    <div className={styles.split}>
      {/* LEFT: AI Generator + Collapsible Draft Editor */}
      <div className={styles.leftColumn}>
        {!aiConnected && (
          <GoogleConnectCard connected={false} onStatusChanged={onAiConnected} />
        )}

        <div className={`panel ${styles.generatorCard}`}>
          <h2 className={styles.cardTitle}>Adaptar Currículo</h2>

          <div className="field">
            <label className="label">Vaga selecionada</label>
            <select
              className="select"
              value={selectedOpportunityId}
               onChange={(e) => {
                 const id = e.target.value;
                 setSelectedOpportunityId(id);
                 setManualDescription('');
                  setDraft(null);
                  setDownloadJob(null);
                  const found = applications.find((item) => item.opportunity_id === id)?.job;
                 if (found) {
                   setManualTitle(found.title);
                   setManualCompany(found.company ?? '');
                 } else {
                   setManualTitle('');
                   setManualCompany('');
                 }
               }}
            >
              <option value="">Colar descrição avulsa abaixo</option>
              {applications.map((card) => (
                <option key={card.opportunity_id} value={card.opportunity_id}>
                  {card.job.title} {card.job.company ? `— ${card.job.company}` : ''} ({card.stage_label})
                </option>
              ))}
            </select>
          </div>

          <div>
            <button
              type="button"
              className={styles.togglePasteBtn}
              onClick={() => setShowManualBox((v) => !v)}
            >
              <span className={`chevron ${showManualBox ? 'chevronOpen' : ''}`}>
                <IconChevronDown size={14} />
              </span>
              <span>
                {selectedOpportunityId
                  ? 'Complementar ou editar texto da vaga'
                  : 'Descrição da vaga'}
              </span>
            </button>
          </div>

          {(showManualBox || !selectedOpportunityId) && (
            <>
              <div className={styles.grid2}>
                <div className="field">
                  <label className="label">Cargo</label>
                  <input
                    className="input"
                    placeholder="Ex.: Analista de Dados Júnior"
                    value={manualTitle}
                    onChange={(e) => setManualTitle(e.target.value)}
                  />
                </div>
                <div className="field">
                  <label className="label">Empresa (opcional)</label>
                  <input
                    className="input"
                    placeholder="Opcional"
                    value={manualCompany}
                    onChange={(e) => setManualCompany(e.target.value)}
                  />
                </div>
              </div>

              <div className="field">
                <label className="label">Descrição e requisitos da vaga</label>
                <textarea
                  rows={4}
                  className="textarea"
                  placeholder="Cole aqui o texto da vaga..."
                  value={manualDescription}
                  onChange={(e) => setManualDescription(e.target.value)}
                />
              </div>
            </>
          )}

          <div className={styles.controlsRow}>
            <div className="field" style={{ width: 135 }}>
              <label className="label">Limite de páginas</label>
              <select
                className="select"
                value={maxPages}
                onChange={(e) => {
                  const next = Number(e.target.value);
                  setMaxPages(next);
                  if (draft) setDraft({ ...draft, max_pages: next });
                }}
              >
                <option value={1}>1 página</option>
                <option value={2}>2 páginas</option>
              </select>
            </div>

            <div className="field" style={{ flex: 1, minWidth: 165 }}>
              <label className="label">Fonte</label>
              <select
                className="select"
                value={draft?.font_family ?? fontFamily}
                onChange={(e) => {
                  const nextFont = e.target.value as ResumeFontFamily;
                  setFontFamily(nextFont);
                  if (draft) setDraft({ ...draft, font_family: nextFont });
                }}
              >
                {FONT_OPTIONS.map((f) => (
                  <option key={f.value} value={f.value}>
                    {f.label}
                  </option>
                ))}
              </select>
            </div>

            <button
              type="button"
              className="btnGhost"
              disabled={generating || loadingSelectedJob || !aiConnected}
              onClick={handleGenerate}
            >
              <IconSparkles size={15} />
              <span>{generating ? 'Gerando rascunho...' : 'Gerar com IA'}</span>
            </button>
          </div>
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
        {downloadJob && (
          <div className="noticeBar">
            {downloadJob.url && !downloadJob.url.startsWith('https://manual.resumatch.local/') && (
              <a className="btnSubtle btnSmall" href={downloadJob.url} target="_blank" rel="noreferrer">Abrir Vaga ↗</a>
            )}
            <button type="button" className="btnGhost btnSmall" onClick={async () => {
              try {
                await api.transitionStage(downloadJob.id, { stage: 'enviada_manualmente', confirm_manual_submission: true });
                setNotice('Candidatura marcada como enviada.');
                setDownloadJob(null);
              } catch (err) { setNotice(err instanceof Error ? err.message : 'Não foi possível marcar a candidatura.'); }
            }}>Já enviei ✓</button>
          </div>
        )}

        {/* Editable Collapsible Sections of Generated ResumeDraft */}
        {draft && (
          <div className={styles.accordionStack}>
            {/* Header quick edit */}
            <div className={styles.accordion}>
              <div className={styles.accordionHead}>
                <button
                  type="button"
                  className={styles.accordionToggle}
                  aria-expanded={Boolean(openSec.header)}
                  onClick={() => toggleOpen('header')}
                >
                  <span className={`chevron ${openSec.header ? 'chevronOpen' : ''}`}>
                    <IconChevronDown size={15} />
                  </span>
                  <span>Cabeçalho e Contatos</span>
                </button>
              </div>
              <div className={`accordionCollapse ${openSec.header ? 'accordionCollapseOpen' : ''}`}>
                <div className="accordionCollapseInner">
                  <div className={styles.accordionContent}>
                    <div className="field">
                      <label className="label">Nome</label>
                      <input
                        className="input"
                        value={draft.header.full_name}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            header: { ...draft.header, full_name: e.target.value },
                          })
                        }
                      />
                    </div>
                    <div className={styles.grid2}>
                      <div className="field">
                        <label className="label">Linha de local</label>
                        <input
                          className="input"
                          value={draft.header.location_line}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              header: { ...draft.header, location_line: e.target.value },
                            })
                          }
                        />
                      </div>
                      <div className="field">
                        <label className="label">Linha de contato</label>
                        <input
                          className="input"
                          value={draft.header.contact_line}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              header: { ...draft.header, contact_line: e.target.value },
                            })
                          }
                        />
                      </div>
                    </div>
                    {draft.header.links.map((lnk, lIdx) => (
                      <div key={lIdx} className={styles.bulletRow}>
                        <input
                          className="input"
                          value={lnk.display_text}
                          onChange={(e) => {
                            const next = [...draft.header.links];
                            next[lIdx] = { ...lnk, display_text: e.target.value };
                            setDraft({ ...draft, header: { ...draft.header, links: next } });
                          }}
                        />
                        <button type="button" className="btnSubtle btnSmall" onClick={() => void copyText(lnk.url)}>
                          Copiar
                        </button>
                        <button
                          type="button"
                          className="iconBtn"
                          title="Remover link do rascunho"
                          onClick={() =>
                            setDraft({
                              ...draft,
                              header: {
                                ...draft.header,
                                links: draft.header.links.filter((_, i) => i !== lIdx),
                              },
                            })
                          }
                        >
                          <IconTrash size={14} />
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>

            {/* Dynamic Ordered Sections */}
            {draft.section_order.map((secKind, secIdx) => (
              <div key={secKind} className={styles.accordion}>
                <div className={styles.accordionHead}>
                  <button
                    type="button"
                    className={styles.accordionToggle}
                    aria-expanded={Boolean(openSec[secKind])}
                    onClick={() => toggleOpen(secKind)}
                  >
                    <span className={`chevron ${openSec[secKind] ? 'chevronOpen' : ''}`}>
                      <IconChevronDown size={15} />
                    </span>
                    <span>{SECTION_NAMES_PT[secKind]}</span>
                  </button>

                  <div className={styles.accordionActions}>
                    <button type="button" className="btnSubtle btnSmall" onClick={() => void restoreSection(secKind)}>
                      ↺ Restaurar do Currículo Base
                    </button>
                    {secKind === 'summary' && (
                      <button type="button" className="btnSubtle btnSmall" onClick={() => void copyText(draft.summary)}>Copiar</button>
                    )}
                    <button
                      type="button"
                      className="iconBtn"
                      disabled={secIdx === 0}
                      title="Subir seção"
                      onClick={() => moveSection(secIdx, -1)}
                    >
                      <IconArrowUp size={14} />
                    </button>
                    <button
                      type="button"
                      className="iconBtn"
                      disabled={secIdx === draft.section_order.length - 1}
                      title="Descer seção"
                      onClick={() => moveSection(secIdx, 1)}
                    >
                      <IconArrowDown size={14} />
                    </button>
                    <button
                      type="button"
                      className="iconBtn"
                      title="Excluir seção inteira deste currículo"
                      onClick={() => deleteEntireSection(secKind)}
                    >
                      <IconTrash size={14} />
                    </button>
                  </div>
                </div>

                <div className={`accordionCollapse ${openSec[secKind] ? 'accordionCollapseOpen' : ''}`}>
                  <div className="accordionCollapseInner">
                    <div className={styles.accordionContent}>
                    {secKind === 'summary' && (
                      <textarea
                        rows={4}
                        className="textarea"
                        value={draft.summary}
                        onChange={(e) => setDraft({ ...draft, summary: e.target.value })}
                      />
                    )}

                    {secKind === 'experience' &&
                      draft.experiences.map((exp, eIdx) => (
                        <div key={exp.id} className={styles.subBlock}>
                          <div className={styles.subHead}>
                            <button type="button" className="btnSubtle btnSmall" onClick={() => void copyText(
                              [exp.role, exp.company, exp.period, exp.description, ...exp.highlights].filter(Boolean).join('\n')
                            )}>Copiar</button>
                            <input
                              className="input"
                              value={exp.role}
                              onChange={(e) => {
                                const next = [...draft.experiences];
                                next[eIdx] = { ...exp, role: e.target.value };
                                setDraft({ ...draft, experiences: next });
                              }}
                            />
                            <button
                              type="button"
                              className="iconBtn"
                              title="Excluir experiência"
                              onClick={() =>
                                setDraft({
                                  ...draft,
                                  experiences: draft.experiences.filter((_, i) => i !== eIdx),
                                })
                              }
                            >
                              <IconTrash size={14} />
                            </button>
                          </div>

                          <textarea
                            rows={2}
                            className="textarea"
                            placeholder="Descrição da experiência"
                            value={exp.description}
                            onChange={(e) => {
                              const next = [...draft.experiences];
                              next[eIdx] = { ...exp, description: e.target.value };
                              setDraft({ ...draft, experiences: next });
                            }}
                          />

                          <div className={styles.grid2}>
                            <input
                              className="input"
                              placeholder="Empresa"
                              value={exp.company}
                              onChange={(e) => {
                                const next = [...draft.experiences];
                                next[eIdx] = { ...exp, company: e.target.value };
                                setDraft({ ...draft, experiences: next });
                              }}
                            />
                            <input
                              className="input"
                              placeholder="Período"
                              value={exp.period}
                              onChange={(e) => {
                                const next = [...draft.experiences];
                                next[eIdx] = { ...exp, period: e.target.value };
                                setDraft({ ...draft, experiences: next });
                              }}
                            />
                          </div>

                          {exp.highlights.map((hl, hIdx) => (
                            <div key={hIdx} className={styles.bulletRow}>
                              <textarea
                                rows={2}
                                className="textarea"
                                style={{ minHeight: 58 }}
                                value={hl}
                                onChange={(e) => {
                                  const nextExps = [...draft.experiences];
                                  const nextHls = [...exp.highlights];
                                  nextHls[hIdx] = e.target.value;
                                  nextExps[eIdx] = { ...exp, highlights: nextHls };
                                  setDraft({ ...draft, experiences: nextExps });
                                }}
                              />
                              <button
                                type="button"
                                className="iconBtn"
                                title="Excluir tópico"
                                onClick={() => {
                                  const nextExps = [...draft.experiences];
                                  nextExps[eIdx] = {
                                    ...exp,
                                    highlights: exp.highlights.filter((_, i) => i !== hIdx),
                                  };
                                  setDraft({ ...draft, experiences: nextExps });
                                }}
                              >
                                <IconTrash size={14} />
                              </button>
                            </div>
                          ))}

                          <div>
                            <button
                              type="button"
                              className="btnSubtle btnSmall"
                              onClick={() => {
                                const nextExps = [...draft.experiences];
                                nextExps[eIdx] = {
                                  ...exp,
                                  highlights: [...exp.highlights, 'Nova entrega realizada.'],
                                };
                                setDraft({ ...draft, experiences: nextExps });
                              }}
                            >
                              <IconPlus size={13} />
                              <span>Adicionar tópico</span>
                            </button>
                          </div>
                        </div>
                      ))}

                    {secKind === 'education' &&
                      draft.education.map((edu, idx) => (
                        <div key={edu.id} className={styles.subBlock}>
                          <div className={styles.subHead}>
                            <input
                              className="input"
                              value={edu.degree}
                              onChange={(e) => {
                                const next = [...draft.education];
                                next[idx] = { ...edu, degree: e.target.value };
                                setDraft({ ...draft, education: next });
                              }}
                            />
                            <button
                              type="button"
                              className="iconBtn"
                              title="Excluir formação"
                              onClick={() =>
                                setDraft({
                                  ...draft,
                                  education: draft.education.filter((_, i) => i !== idx),
                                })
                              }
                            >
                              <IconTrash size={14} />
                            </button>
                          </div>
                          <div className={styles.grid2}>
                            <input
                              className="input"
                              value={edu.institution}
                              onChange={(e) => {
                                const next = [...draft.education];
                                next[idx] = { ...edu, institution: e.target.value };
                                setDraft({ ...draft, education: next });
                              }}
                            />
                            <input
                              className="input"
                              value={edu.period}
                              onChange={(e) => {
                                const next = [...draft.education];
                                next[idx] = { ...edu, period: e.target.value };
                                setDraft({ ...draft, education: next });
                              }}
                            />
                          </div>
                        </div>
                      ))}

                    {secKind === 'projects' &&
                      draft.projects.map((prj, idx) => (
                        <div key={prj.id} className={styles.subBlock}>
                          <div className={styles.subHead}>
                            <input
                              className="input"
                              placeholder="Nome do projeto"
                              value={prj.name}
                              onChange={(e) => {
                                const next = [...draft.projects];
                                next[idx] = { ...prj, name: e.target.value };
                                setDraft({ ...draft, projects: next });
                              }}
                            />
                            <button
                              type="button"
                              className="iconBtn"
                              title="Excluir projeto"
                              onClick={() =>
                                setDraft({
                                  ...draft,
                                  projects: draft.projects.filter((_, i) => i !== idx),
                                })
                              }
                            >
                              <IconTrash size={14} />
                            </button>
                          </div>
                          <input
                            className="input"
                            placeholder="Link opcional (https://)"
                            value={prj.url ?? ''}
                            onChange={(e) => {
                              const next = [...draft.projects];
                              next[idx] = { ...prj, url: e.target.value.trim() || null };
                              setDraft({ ...draft, projects: next });
                            }}
                          />
                          <textarea
                            rows={2}
                            className="textarea"
                            style={{ minHeight: 58 }}
                            placeholder="Descrição do projeto"
                            value={prj.description}
                            onChange={(e) => {
                              const next = [...draft.projects];
                              next[idx] = { ...prj, description: e.target.value };
                              setDraft({ ...draft, projects: next });
                            }}
                          />
                          <textarea
                            rows={2}
                            className="textarea"
                            placeholder="Destaques do projeto (um por linha)"
                            value={prj.highlights.join('\n')}
                            onChange={(e) => {
                              const next = [...draft.projects];
                              next[idx] = { ...prj, highlights: e.target.value.split('\n') };
                              setDraft({ ...draft, projects: next });
                            }}
                          />
                        </div>
                      ))}

                    {secKind === 'courses' &&
                      draft.courses.map((crs, idx) => (
                        <div key={crs.id} className={styles.subBlock}>
                          <div className={styles.subHead}>
                            <input
                              className="input"
                              placeholder="Título do curso ou certificação"
                              value={crs.title}
                              onChange={(e) => {
                                const next = [...draft.courses];
                                next[idx] = { ...crs, title: e.target.value };
                                setDraft({ ...draft, courses: next });
                              }}
                            />
                            <button
                              type="button"
                              className="iconBtn"
                              title="Excluir curso"
                              onClick={() =>
                                setDraft({
                                  ...draft,
                                  courses: draft.courses.filter((_, i) => i !== idx),
                                })
                              }
                            >
                              <IconTrash size={14} />
                            </button>
                          </div>
                          <input
                            className="input"
                            placeholder="Instituição"
                            value={crs.institution}
                            onChange={(e) => {
                              const next = [...draft.courses];
                              next[idx] = { ...crs, institution: e.target.value };
                              setDraft({ ...draft, courses: next });
                            }}
                          />
                          <div className={styles.grid2}>
                            <input
                              className="input"
                              placeholder="Carga Horária"
                              value={crs.workload}
                              onChange={(e) => {
                                const next = [...draft.courses];
                                next[idx] = { ...crs, workload: e.target.value };
                                setDraft({ ...draft, courses: next });
                              }}
                            />
                            <input
                              className="input"
                              placeholder="Conclusão"
                              value={crs.completion_date}
                              onChange={(e) => {
                                const next = [...draft.courses];
                                next[idx] = { ...crs, completion_date: e.target.value };
                                setDraft({ ...draft, courses: next });
                              }}
                            />
                          </div>
                          <input
                            className="input"
                            placeholder="Descrição opcional"
                            value={crs.description}
                            onChange={(e) => {
                              const next = [...draft.courses];
                              next[idx] = { ...crs, description: e.target.value };
                              setDraft({ ...draft, courses: next });
                            }}
                          />
                        </div>
                      ))}

                    {secKind === 'skills' &&
                      draft.skill_groups.map((grp, gIdx) => (
                        <div key={grp.id} className={styles.subBlock}>
                          <div className={styles.subHead}>
                            <input
                              className="input"
                              placeholder="Categoria"
                              value={grp.label}
                              onChange={(e) => {
                                const next = [...draft.skill_groups];
                                next[gIdx] = { ...grp, label: e.target.value };
                                setDraft({ ...draft, skill_groups: next });
                              }}
                            />
                            <button
                              type="button"
                              className="iconBtn"
                              title="Excluir grupo de competências"
                              onClick={() =>
                                setDraft({
                                  ...draft,
                                  skill_groups: draft.skill_groups.filter((_, i) => i !== gIdx),
                                })
                              }
                            >
                              <IconTrash size={14} />
                            </button>
                          </div>
                          <input
                            className="input"
                            placeholder="Competências separadas por vírgula"
                            value={skillInputs[grp.id] ?? grp.items.join(', ')}
                            onChange={(e) => {
                              setSkillInputs((current) => ({ ...current, [grp.id]: e.target.value }));
                              const next = [...draft.skill_groups];
                              next[gIdx] = {
                                ...grp,
                                items: e.target.value
                                  .split(',')
                                  .map((s) => s.trim())
                                  .filter(Boolean),
                              };
                              setDraft({ ...draft, skill_groups: next });
                            }}
                            onBlur={() => setSkillInputs((current) => {
                              const next = { ...current };
                              delete next[grp.id];
                              return next;
                            })}
                          />
                        </div>
                      ))}

                    {secKind === 'languages' &&
                      draft.languages.map((lng, idx) => (
                        <div key={lng.id} className={styles.bulletRow}>
                          <input
                            className="input"
                            value={lng.language}
                            onChange={(e) => {
                              const next = [...draft.languages];
                              next[idx] = { ...lng, language: e.target.value };
                              setDraft({ ...draft, languages: next });
                            }}
                          />
                          <input
                            className="input"
                            value={lng.proficiency}
                            onChange={(e) => {
                              const next = [...draft.languages];
                              next[idx] = { ...lng, proficiency: e.target.value };
                              setDraft({ ...draft, languages: next });
                            }}
                          />
                          <button
                            type="button"
                            className="iconBtn"
                            title="Excluir idioma"
                            onClick={() =>
                              setDraft({
                                ...draft,
                                languages: draft.languages.filter((_, i) => i !== idx),
                              })
                            }
                          >
                            <IconTrash size={14} />
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* RIGHT: Live A4 Preview + Download PDF */}
      <div className={styles.rightColumn}>
        <div className={`panel ${styles.previewBar}`}>
          <div className={styles.previewMeta}>
            <span>Folha A4</span>
            {draft && (
              <span>
                ·{' '}
                {isOverflow
                  ? `Ajuste automático p/ ${activeMaxPages} pág. ao baixar`
                  : `Dentro de ${activeMaxPages} página(s)`}
              </span>
            )}
          </div>

          <div className={styles.zoomRow}>
            {[0.65, 0.72, 0.88].map((z) => (
              <button
                key={z}
                type="button"
                className={`btnSubtle btnSmall ${zoom === z ? styles.zoomBtnActive : ''}`}
                onClick={() => setZoom(z)}
              >
                {Math.round(z * 100)}%
              </button>
            ))}

            <button
              type="button"
              className="btnGhost"
              disabled={!draft || exporting}
              onClick={handleExportAndDownload}
            >
              <IconDownload size={15} />
              <span>{exporting ? 'Gerando PDF...' : 'Baixar PDF'}</span>
            </button>
          </div>
        </div>

        <div className={styles.stage}>
          {!previewHtml ? (
            <div className={styles.emptyStage}>
              Selecione uma vaga ou cole a descrição à esquerda e clique em{' '}
              <strong>Gerar com IA</strong> para visualizar e editar sua Folha A4 ao vivo.
            </div>
          ) : (
            <div
              style={{
                width: `${794 * zoom}px`,
                height: `${iframeHeight * zoom}px`,
              }}
            >
              <div
                style={{
                  width: '794px',
                  transform: `scale(${zoom})`,
                  transformOrigin: 'top left',
                  position: 'relative',
                }}
              >
                <iframe
                  ref={iframeRef}
                  title="Pré-visualização A4"
                  srcDoc={previewHtml}
                  className={styles.a4Iframe}
                  style={{ height: `${iframeHeight}px` }}
                  onLoad={measureIframe}
                />
                <div
                  className={styles.pageLimitRule}
                  style={{ top: `${limitRuleTopPx}px` }}
                />
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
