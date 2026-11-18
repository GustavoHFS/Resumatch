import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api, createEmptyMasterProfile, normalizeMasterProfile } from '../api/client';
import {
  IconChevronDown,
  IconClose,
  IconFile,
  IconPlus,
  IconTrash,
  IconUpload,
} from '../components/Icons';
import type { MasterProfile } from '../types/api';
import styles from './BaseResumePage.module.css';

interface Props {
  profile: MasterProfile | null;
  onProfileSaved: (updated: MasterProfile) => void;
}

type SectionKey =
  | 'personal'
  | 'summary'
  | 'experiences'
  | 'education'
  | 'courses'
  | 'projects'
  | 'skills'
  | 'languages';

interface AccordionProps {
  title: string;
  meta: string;
  isOpen: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}

const AccordionBox: React.FC<AccordionProps> = ({ title, meta, isOpen, onToggle, children }) => (
  <section className={styles.accordion}>
    <button
      type="button"
      className={styles.accordionTrigger}
      aria-expanded={isOpen}
      onClick={onToggle}
    >
      <div className={styles.accordionLeft}>
        <h2 className={styles.accordionTitle}>{title}</h2>
        <span className={styles.countText}>{meta}</span>
      </div>
      <span className={`chevron ${isOpen ? 'chevronOpen' : ''}`}>
        <IconChevronDown size={16} />
      </span>
    </button>
    <div className={`accordionCollapse ${isOpen ? 'accordionCollapseOpen' : ''}`}>
      <div className="accordionCollapseInner">
        <div className={styles.accordionBody}>{children}</div>
      </div>
    </div>
  </section>
);

const makeId = (prefix: string) => `${prefix}_${Math.random().toString(36).slice(2, 9)}`;

export const BaseResumePage: React.FC<Props> = ({ profile, onProfileSaved }) => {
  const [draft, setDraft] = useState<MasterProfile | null>(
    profile ? normalizeMasterProfile(profile) : null
  );
  const [loading, setLoading] = useState<boolean>(!profile);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [openSections, setOpenSections] = useState<Record<SectionKey, boolean>>({
    personal: true,
    summary: true,
    experiences: true,
    education: true,
    courses: false,
    projects: false,
    skills: true,
    languages: false,
  });

  const [newSkillText, setNewSkillText] = useState('');
  const [saving, setSaving] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importingFileName, setImportingFileName] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const fetchProfileDirectly = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const fresh = await api.getProfile();
      setDraft(fresh);
      onProfileSaved(fresh);
    } catch (err) {
      setLoadError(
        err instanceof Error
          ? err.message
          : 'Não foi possível carregar o Currículo Base local.'
      );
    } finally {
      setLoading(false);
    }
  }, [onProfileSaved]);

  useEffect(() => {
    if (profile) {
      setDraft(normalizeMasterProfile(profile));
      setLoadError(null);
      setLoading(false);
    } else {
      void fetchProfileDirectly();
    }
  }, [profile, fetchProfileDirectly]);

  const toggleSection = (key: SectionKey) => {
    setOpenSections((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const handleUploadFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;

    setImporting(true);
    setImportingFileName(file.name);
    setNotice(null);
    try {
      const res = await api.importProfileFile(file);
      setDraft(res.profile_draft);
      setOpenSections({
        personal: true,
        summary: true,
        experiences: true,
        education: true,
        courses: res.profile_draft.courses.length > 0,
        projects: res.profile_draft.projects.length > 0,
        skills: true,
        languages: res.profile_draft.languages.length > 0,
      });
      const warnSuffix = res.warnings.length > 0 ? ` (${res.warnings[0]})` : '';
      setNotice(
        `Blocos importados de "${file.name}". Revise os campos abaixo e clique em Salvar Alterações.${warnSuffix}`
      );
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Falha ao importar o arquivo.');
    } finally {
      setImporting(false);
      setImportingFileName(null);
    }
  };

  const handleSave = async () => {
    if (!draft) return;
    setSaving(true);
    setNotice(null);
    try {
      const res = await api.saveProfile(draft);
      setDraft(res.profile);
      onProfileSaved(res.profile);
      setNotice('Currículo Base salvo com sucesso.');
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Verifique os campos preenchidos.');
    } finally {
      setSaving(false);
    }
  };

  const addSkillTag = () => {
    if (!draft) return;
    const rawParts = newSkillText
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    if (rawParts.length === 0) return;

    const existingLower = new Set(draft.skills.map((s) => s.toLowerCase()));
    const nextSkills = [...draft.skills];
    for (const item of rawParts) {
      if (!existingLower.has(item.toLowerCase())) {
        existingLower.add(item.toLowerCase());
        nextSkills.push(item);
      }
    }
    setDraft({ ...draft, skills: nextSkills });
    setNewSkillText('');
  };

  if (loading && !draft) {
    return (
      <div className={styles.page}>
        <div className={`panel ${styles.statusCard}`}>
          <p>Carregando Currículo Base...</p>
        </div>
      </div>
    );
  }

  if (!draft) {
    return (
      <div className={styles.page}>
        <div className={`panel ${styles.statusCard}`}>
          <h2 className={styles.importTitle}>Não foi possível carregar o Currículo Base</h2>
          <p className={styles.importDesc}>
            {loadError ?? 'Verifique se o servidor local do Resumatch está ativo.'}
          </p>
          <div className={styles.headerActions}>
            <button
              type="button"
              className="btnGhost"
              onClick={() => void fetchProfileDirectly()}
            >
              Tentar novamente
            </button>
            <button
              type="button"
              className="btnSubtle"
              onClick={() => {
                setLoadError(null);
                setDraft(createEmptyMasterProfile());
              }}
            >
              Iniciar currículo em branco
            </button>
          </div>
        </div>
      </div>
    );
  }

  const safeLinks = draft.header?.links ?? [];
  const safeExperiences = draft.experiences ?? [];
  const safeEducation = draft.education ?? [];
  const safeCourses = draft.courses ?? [];
  const safeProjects = draft.projects ?? [];
  const safeSkills = draft.skills ?? [];
  const safeLanguages = draft.languages ?? [];

  return (
    <div className={styles.page}>
      <div className={styles.headerRow}>
        <div>
          <h1 className={styles.pageTitle}>Currículo Base</h1>
          <p className={styles.pageSubtitle}>
            Mantenha todo o seu histórico completo aqui; o Estúdio adapta automaticamente para cada vaga.
          </p>
        </div>

        <div className={styles.headerActions}>
          <button type="button" className="btnGhost" disabled={saving} onClick={handleSave}>
            {saving ? 'Salvando...' : 'Salvar Alterações'}
          </button>
        </div>
      </div>

      {/* Highlighted Smart Block-Grouping Import Card */}
      <div className={styles.importBanner}>
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,.docx"
          className={styles.hiddenFile}
          onChange={handleUploadFile}
        />
        <div className={styles.importInfo}>
          <span className={styles.importIconWrap}>
            <IconFile size={18} />
          </span>
          <div>
            <h2 className={styles.importTitle}>
              {importing
                ? `Agrupando blocos de "${importingFileName}"...`
                : 'Importar Currículo Existente (.PDF ou .DOCX)'}
            </h2>
            <p className={styles.importDesc}>
              {importing
                ? 'Identificando formação acadêmica, experiências, cursos, competências e contatos em blocos completos.'
                : 'Envie seu arquivo atual para preencher automaticamente formação, experiências, cursos e competências.'}
            </p>
          </div>
        </div>
        <button
          type="button"
          className="btnSubtle"
          disabled={importing}
          onClick={() => fileInputRef.current?.click()}
        >
          <IconUpload size={15} />
          <span>{importing ? 'Lendo blocos...' : 'Selecionar PDF ou DOCX'}</span>
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

      <div className={styles.accordionList}>
        {/* 1. DADOS PESSOAIS */}
        <AccordionBox
          title="Dados Pessoais"
          meta={`${safeLinks.length} link(s)`}
          isOpen={openSections.personal}
          onToggle={() => toggleSection('personal')}
        >
          <div className={styles.grid3}>
            <div className="field">
              <label className="label">Nome completo</label>
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
            <div className="field">
              <label className="label">E-mail</label>
              <input
                className="input"
                placeholder="Opcional"
                value={draft.header.email}
                onChange={(e) =>
                  setDraft({ ...draft, header: { ...draft.header, email: e.target.value } })
                }
              />
            </div>
            <div className="field">
              <label className="label">Telefone</label>
              <input
                className="input"
                placeholder="Opcional"
                value={draft.header.phone}
                onChange={(e) =>
                  setDraft({ ...draft, header: { ...draft.header, phone: e.target.value } })
                }
              />
            </div>
          </div>

          <div className={styles.grid3}>
            <div className="field">
              <label className="label">Cidade</label>
              <input
                className="input"
                value={draft.header.city}
                onChange={(e) =>
                  setDraft({ ...draft, header: { ...draft.header, city: e.target.value } })
                }
              />
            </div>
            <div className="field">
              <label className="label">Estado</label>
              <input
                className="input"
                value={draft.header.state}
                onChange={(e) =>
                  setDraft({ ...draft, header: { ...draft.header, state: e.target.value } })
                }
              />
            </div>
            <div className="field">
              <label className="label">Bairro (opcional)</label>
              <input
                className="input"
                placeholder="Opcional"
                value={draft.header.neighborhood ?? ''}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    header: { ...draft.header, neighborhood: e.target.value || null },
                  })
                }
              />
            </div>
          </div>

          <div className={styles.itemTopBar}>
            <span className="label">Links (GitHub, LinkedIn, Portfólio)</span>
            <button
              type="button"
              className="btnSubtle btnSmall"
              onClick={() =>
                setDraft({
                  ...draft,
                  header: {
                    ...draft.header,
                    links: [
                      ...draft.header.links,
                      {
                        label: 'LinkedIn',
                        url: 'https://linkedin.com/in/seu-perfil',
                        display_text: 'linkedin.com/in/seu-perfil',
                      },
                    ],
                  },
                })
              }
            >
              <IconPlus size={14} />
              <span>Adicionar link</span>
            </button>
          </div>

          {safeLinks.map((lnk, idx) => (
            <div key={idx} className={styles.itemBlock}>
              <div className={styles.itemTopBar}>
                <span className={styles.itemLabel}>Link {idx + 1}</span>
                <button
                  type="button"
                  className="iconBtn"
                  title="Excluir link"
                  onClick={() =>
                    setDraft({
                      ...draft,
                      header: {
                        ...draft.header,
                        links: draft.header.links.filter((_, i) => i !== idx),
                      },
                    })
                  }
                >
                  <IconTrash size={15} />
                </button>
              </div>
              <div className={styles.grid3}>
                <div className="field">
                  <label className="label">Nome</label>
                  <input
                    className="input"
                    value={lnk.label}
                    onChange={(e) => {
                      const next = [...draft.header.links];
                      next[idx] = { ...lnk, label: e.target.value };
                      setDraft({ ...draft, header: { ...draft.header, links: next } });
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">URL (https://)</label>
                  <input
                    className="input"
                    value={lnk.url}
                    onChange={(e) => {
                      const next = [...draft.header.links];
                      next[idx] = { ...lnk, url: e.target.value };
                      setDraft({ ...draft, header: { ...draft.header, links: next } });
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">Texto exibido</label>
                  <input
                    className="input"
                    value={lnk.display_text}
                    onChange={(e) => {
                      const next = [...draft.header.links];
                      next[idx] = { ...lnk, display_text: e.target.value };
                      setDraft({ ...draft, header: { ...draft.header, links: next } });
                    }}
                  />
                </div>
              </div>
            </div>
          ))}
        </AccordionBox>

        {/* 2. RESUMO GERAL */}
        <AccordionBox
          title="Resumo Geral"
          meta={draft.summary.trim() ? 'Preenchido' : 'Vazio'}
          isOpen={openSections.summary}
          onToggle={() => toggleSection('summary')}
        >
          <div className={styles.itemTopBar}>
            <span className="label">Apresentação livre sobre sua trajetória e objetivos</span>
            {draft.summary && (
              <button
                type="button"
                className="iconBtn"
                title="Limpar resumo"
                onClick={() => setDraft({ ...draft, summary: '' })}
              >
                <IconTrash size={15} />
              </button>
            )}
          </div>
          <textarea
            rows={4}
            className="textarea"
            placeholder="Descreva sua área, formação e principais pontos fortes..."
            value={draft.summary}
            onChange={(e) => setDraft({ ...draft, summary: e.target.value })}
          />
        </AccordionBox>

        {/* 3. EXPERIÊNCIAS PROFISSIONAIS */}
        <AccordionBox
          title="Experiências Profissionais"
          meta={String(safeExperiences.length)}
          isOpen={openSections.experiences}
          onToggle={() => toggleSection('experiences')}
        >
          <div>
            <button
              type="button"
              className="btnSubtle btnSmall"
              onClick={() =>
                setDraft({
                  ...draft,
                  experiences: [
                    {
                      id: makeId('exp'),
                      role: '',
                      company: '',
                      period: '',
                      location: '',
                      description: '',
                    },
                    ...draft.experiences,
                  ],
                })
              }
            >
              <IconPlus size={14} />
              <span>Adicionar experiência</span>
            </button>
          </div>

          {safeExperiences.map((exp, idx) => (
            <div key={exp.id} className={styles.itemBlock}>
              <div className={styles.itemTopBar}>
                <span className={styles.itemLabel}>
                  {exp.role || `Experiência ${idx + 1}`}
                  {exp.company ? ` · ${exp.company}` : ''}
                </span>
                <button
                  type="button"
                  className="iconBtn"
                  title="Excluir experiência"
                  onClick={() =>
                    setDraft({
                      ...draft,
                      experiences: draft.experiences.filter((_, i) => i !== idx),
                    })
                  }
                >
                  <IconTrash size={15} />
                </button>
              </div>

              <div className={styles.grid2}>
                <div className="field">
                  <label className="label">Cargo</label>
                  <input
                    className="input"
                    placeholder="Ex.: Analista de Suporte"
                    value={exp.role}
                    onChange={(e) => {
                      const next = [...draft.experiences];
                      next[idx] = { ...exp, role: e.target.value };
                      setDraft({ ...draft, experiences: next });
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">Empresa</label>
                  <input
                    className="input"
                    placeholder="Nome da empresa"
                    value={exp.company}
                    onChange={(e) => {
                      const next = [...draft.experiences];
                      next[idx] = { ...exp, company: e.target.value };
                      setDraft({ ...draft, experiences: next });
                    }}
                  />
                </div>
              </div>

              <div className={styles.grid2}>
                <div className="field">
                  <label className="label">Período</label>
                  <input
                    className="input"
                    placeholder="Ex.: Jan/2024 – Atual"
                    value={exp.period}
                    onChange={(e) => {
                      const next = [...draft.experiences];
                      next[idx] = { ...exp, period: e.target.value };
                      setDraft({ ...draft, experiences: next });
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">Local</label>
                  <input
                    className="input"
                    placeholder="Ex.: Recife, PE ou Remoto"
                    value={exp.location}
                    onChange={(e) => {
                      const next = [...draft.experiences];
                      next[idx] = { ...exp, location: e.target.value };
                      setDraft({ ...draft, experiences: next });
                    }}
                  />
                </div>
              </div>

              <div className="field">
                <label className="label">
                  O que você fazia na função (descreva livremente suas rotinas, ferramentas e
                  entregas)
                </label>
                <textarea
                  className={`textarea ${styles.richTextarea}`}
                  placeholder="Conte em texto corrido tudo o que realizava, ferramentas usadas e resultados alcançados..."
                  value={exp.description}
                  onChange={(e) => {
                    const next = [...draft.experiences];
                    next[idx] = { ...exp, description: e.target.value };
                    setDraft({ ...draft, experiences: next });
                  }}
                />
              </div>
            </div>
          ))}
        </AccordionBox>

        {/* 4. FORMAÇÃO ACADÊMICA */}
        <AccordionBox
          title="Formação Acadêmica"
          meta={String(safeEducation.length)}
          isOpen={openSections.education}
          onToggle={() => toggleSection('education')}
        >
          <div>
            <button
              type="button"
              className="btnSubtle btnSmall"
              onClick={() =>
                setDraft({
                  ...draft,
                  education: [
                    ...draft.education,
                    { id: makeId('edu'), degree: '', institution: '', period: '', details: '' },
                  ],
                })
              }
            >
              <IconPlus size={14} />
              <span>Adicionar formação</span>
            </button>
          </div>

          {safeEducation.map((edu, idx) => (
            <div key={edu.id} className={styles.itemBlock}>
              <div className={styles.itemTopBar}>
                <span className={styles.itemLabel}>{edu.degree || `Formação ${idx + 1}`}</span>
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
                  <IconTrash size={15} />
                </button>
              </div>

              <div className={styles.grid2}>
                <div className="field">
                  <label className="label">Curso / Grau</label>
                  <input
                    className="input"
                    value={edu.degree}
                    onChange={(e) => {
                      const next = [...draft.education];
                      next[idx] = { ...edu, degree: e.target.value };
                      setDraft({ ...draft, education: next });
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">Instituição</label>
                  <input
                    className="input"
                    value={edu.institution}
                    onChange={(e) => {
                      const next = [...draft.education];
                      next[idx] = { ...edu, institution: e.target.value };
                      setDraft({ ...draft, education: next });
                    }}
                  />
                </div>
              </div>

              <div className={styles.grid2}>
                <div className="field">
                  <label className="label">Período / Situação</label>
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
                <div className="field">
                  <label className="label">Observação opcional</label>
                  <input
                    className="input"
                    placeholder="Opcional"
                    value={edu.details}
                    onChange={(e) => {
                      const next = [...draft.education];
                      next[idx] = { ...edu, details: e.target.value };
                      setDraft({ ...draft, education: next });
                    }}
                  />
                </div>
              </div>
            </div>
          ))}
        </AccordionBox>

        {/* 5. CURSOS E CERTIFICAÇÕES */}
        <AccordionBox
          title="Cursos e Certificações"
          meta={String(safeCourses.length)}
          isOpen={openSections.courses}
          onToggle={() => toggleSection('courses')}
        >
          <div>
            <button
              type="button"
              className="btnSubtle btnSmall"
              onClick={() =>
                setDraft({
                  ...draft,
                  courses: [
                    ...draft.courses,
                    {
                      id: makeId('crs'),
                      title: '',
                      institution: '',
                      workload: '',
                      completion_date: '',
                      description: '',
                    },
                  ],
                })
              }
            >
              <IconPlus size={14} />
              <span>Adicionar curso ou certificação</span>
            </button>
          </div>

          {safeCourses.map((crs, idx) => (
            <div key={crs.id} className={styles.itemBlock}>
              <div className={styles.itemTopBar}>
                <span className={styles.itemLabel}>{crs.title || `Curso ${idx + 1}`}</span>
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
                  <IconTrash size={15} />
                </button>
              </div>

              <div className={styles.grid2}>
                <div className="field">
                  <label className="label">Título</label>
                  <input
                    className="input"
                    value={crs.title}
                    onChange={(e) => {
                      const next = [...draft.courses];
                      next[idx] = { ...crs, title: e.target.value };
                      setDraft({ ...draft, courses: next });
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">Instituição</label>
                  <input
                    className="input"
                    value={crs.institution}
                    onChange={(e) => {
                      const next = [...draft.courses];
                      next[idx] = { ...crs, institution: e.target.value };
                      setDraft({ ...draft, courses: next });
                    }}
                  />
                </div>
              </div>

              <div className={styles.grid2}>
                <div className="field">
                  <label className="label">Carga Horária</label>
                  <input
                    className="input"
                    placeholder="Ex.: 40 horas"
                    value={crs.workload}
                    onChange={(e) => {
                      const next = [...draft.courses];
                      next[idx] = { ...crs, workload: e.target.value };
                      setDraft({ ...draft, courses: next });
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">Conclusão</label>
                  <input
                    className="input"
                    placeholder="Ex.: Out/2026 ou 2026"
                    value={crs.completion_date}
                    onChange={(e) => {
                      const next = [...draft.courses];
                      next[idx] = { ...crs, completion_date: e.target.value };
                      setDraft({ ...draft, courses: next });
                    }}
                  />
                </div>
              </div>

              <div className="field">
                <label className="label">Descrição</label>
                <textarea
                  rows={2}
                  className="textarea"
                  style={{ minHeight: 68 }}
                  placeholder="Conteúdos estudados, tecnologias aplicadas ou projeto prático desenvolvido..."
                  value={crs.description}
                  onChange={(e) => {
                    const next = [...draft.courses];
                    next[idx] = { ...crs, description: e.target.value };
                    setDraft({ ...draft, courses: next });
                  }}
                />
              </div>
            </div>
          ))}
        </AccordionBox>

        {/* 6. PROJETOS */}
        <AccordionBox
          title="Projetos"
          meta={String(safeProjects.length)}
          isOpen={openSections.projects}
          onToggle={() => toggleSection('projects')}
        >
          <div>
            <button
              type="button"
              className="btnSubtle btnSmall"
              onClick={() =>
                setDraft({
                  ...draft,
                  projects: [
                    ...draft.projects,
                    {
                      id: makeId('prj'),
                      name: '',
                      url: null,
                      description: '',
                    },
                  ],
                })
              }
            >
              <IconPlus size={14} />
              <span>Adicionar projeto</span>
            </button>
          </div>

          {safeProjects.map((prj, idx) => (
            <div key={prj.id} className={styles.itemBlock}>
              <div className={styles.itemTopBar}>
                <span className={styles.itemLabel}>{prj.name || `Projeto ${idx + 1}`}</span>
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
                  <IconTrash size={15} />
                </button>
              </div>

              <div className={styles.grid2}>
                <div className="field">
                  <label className="label">Nome do projeto</label>
                  <input
                    className="input"
                    value={prj.name}
                    onChange={(e) => {
                      const next = [...draft.projects];
                      next[idx] = { ...prj, name: e.target.value };
                      setDraft({ ...draft, projects: next });
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">Link opcional (https://)</label>
                  <input
                    className="input"
                    placeholder="https://github.com/..."
                    value={prj.url ?? ''}
                    onChange={(e) => {
                      const next = [...draft.projects];
                      next[idx] = { ...prj, url: e.target.value.trim() || null };
                      setDraft({ ...draft, projects: next });
                    }}
                  />
                </div>
              </div>

              <div className="field">
                <label className="label">Descrição do projeto</label>
                <textarea
                  rows={3}
                  className="textarea"
                  value={prj.description}
                  onChange={(e) => {
                    const next = [...draft.projects];
                    next[idx] = { ...prj, description: e.target.value };
                    setDraft({ ...draft, projects: next });
                  }}
                />
              </div>
            </div>
          ))}
        </AccordionBox>

        {/* 7. COMPETÊNCIAS */}
        <AccordionBox
          title="Competências"
          meta={String(safeSkills.length)}
          isOpen={openSections.skills}
          onToggle={() => toggleSection('skills')}
        >
          <div className={styles.tagInputRow}>
            <input
              className="input"
              placeholder="Digite competências separadas por vírgula e pressione Enter (ex.: Python, SQL, Active Directory)..."
              value={newSkillText}
              onChange={(e) => setNewSkillText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  addSkillTag();
                }
              }}
            />
            <button type="button" className="btnSubtle" onClick={addSkillTag}>
              Adicionar
            </button>
          </div>

          {safeSkills.length > 0 && (
            <div className={styles.tagsGrid}>
              {safeSkills.map((skill, idx) => (
                <span key={`${skill}-${idx}`} className={styles.skillChip}>
                  <span>{skill}</span>
                  <button
                    type="button"
                    className={styles.chipRemove}
                    title={`Remover ${skill}`}
                    onClick={() =>
                      setDraft({
                        ...draft,
                        skills: draft.skills.filter((_, i) => i !== idx),
                      })
                    }
                  >
                    <IconClose size={13} />
                  </button>
                </span>
              ))}
            </div>
          )}
        </AccordionBox>

        {/* 8. IDIOMAS */}
        <AccordionBox
          title="Idiomas"
          meta={String(safeLanguages.length)}
          isOpen={openSections.languages}
          onToggle={() => toggleSection('languages')}
        >
          <div>
            <button
              type="button"
              className="btnSubtle btnSmall"
              onClick={() =>
                setDraft({
                  ...draft,
                  languages: [
                    ...draft.languages,
                    { id: makeId('lng'), language: '', proficiency: '' },
                  ],
                })
              }
            >
              <IconPlus size={14} />
              <span>Adicionar idioma</span>
            </button>
          </div>

          {safeLanguages.map((lng, idx) => (
            <div key={lng.id} className={styles.itemBlock}>
              <div className={styles.itemTopBar}>
                <span className={styles.itemLabel}>{lng.language || `Idioma ${idx + 1}`}</span>
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
                  <IconTrash size={15} />
                </button>
              </div>
              <div className={styles.grid2}>
                <div className="field">
                  <label className="label">Idioma</label>
                  <input
                    className="input"
                    placeholder="Ex.: Inglês"
                    value={lng.language}
                    onChange={(e) => {
                      const next = [...draft.languages];
                      next[idx] = { ...lng, language: e.target.value };
                      setDraft({ ...draft, languages: next });
                    }}
                  />
                </div>
                <div className="field">
                  <label className="label">Nível</label>
                  <input
                    className="input"
                    placeholder="Ex.: Avançado"
                    value={lng.proficiency}
                    onChange={(e) => {
                      const next = [...draft.languages];
                      next[idx] = { ...lng, proficiency: e.target.value };
                      setDraft({ ...draft, languages: next });
                    }}
                  />
                </div>
              </div>
            </div>
          ))}
        </AccordionBox>
      </div>
    </div>
  );
};
