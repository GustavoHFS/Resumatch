import React, { useEffect, useState } from 'react';
import { api } from '../api/client';
import type {
  BlockedCompaniesConfig,
  BulletItem,
  JobSourceItem,
  LlmConfigResponse,
  MasterProfile,
  SearchCriteria,
  SkillItem,
  VeracityState,
} from '../types/api';
import styles from './ProfileSettingsPage.module.css';

interface Props {
  profile: MasterProfile | null;
  onProfileSaved: (updated: MasterProfile) => void;
  onRefreshHealth: () => void;
}

type SettingsTab =
  | 'header'
  | 'experiences'
  | 'skills'
  | 'education_courses'
  | 'presets'
  | 'criteria'
  | 'blocked_sources'
  | 'llm';

const splitCsv = (val: string): string[] =>
  val
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

export const ProfileSettingsPage: React.FC<Props> = ({
  profile,
  onProfileSaved,
  onRefreshHealth,
}) => {
  const [activeTab, setActiveTab] = useState<SettingsTab>('header');
  const [draftProfile, setDraftProfile] = useState<MasterProfile | null>(
    profile ? (JSON.parse(JSON.stringify(profile)) as MasterProfile) : null
  );

  const [criteria, setCriteria] = useState<SearchCriteria | null>(null);
  const [blocked, setBlocked] = useState<BlockedCompaniesConfig | null>(null);
  const [sources, setSources] = useState<JobSourceItem[]>([]);
  const [llmConfig, setLlmConfig] = useState<LlmConfigResponse | null>(null);

  const [geminiKeyInput, setGeminiKeyInput] = useState('');
  const [groqKeyInput, setGroqKeyInput] = useState('');

  const [skillFilterVeracity, setSkillFilterVeracity] = useState<string>('');
  const [skillSearch, setSkillSearch] = useState<string>('');
  const [selectedPresetKey, setSelectedPresetKey] = useState<string>('');

  const [saving, setSaving] = useState(false);
  const [banner, setBanner] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  useEffect(() => {
    if (profile) {
      const cloned = JSON.parse(JSON.stringify(profile)) as MasterProfile;
      setDraftProfile(cloned);
      const firstKey = Object.keys(cloned.area_presets)[0] ?? '';
      setSelectedPresetKey((prev) => (prev && cloned.area_presets[prev] ? prev : firstKey));
    }
  }, [profile]);

  useEffect(() => {
    Promise.all([
      api.getCriteria(),
      api.getBlocked(),
      api.getSources(),
      api.getLlmConfig(),
    ])
      .then(([critRes, blkRes, srcRes, llmRes]) => {
        setCriteria(critRes);
        setBlocked(blkRes);
        setSources(srcRes.sources);
        setLlmConfig(llmRes);
      })
      .catch(() => {});
  }, []);

  const handleSaveMasterProfile = async () => {
    if (!draftProfile) return;
    setSaving(true);
    setBanner(null);
    try {
      const res = await api.saveProfile(draftProfile);
      onProfileSaved(res.profile);
      setBanner({
        type: 'ok',
        text: `Perfil Mestre salvo atomicamente! Snapshot de segurança: ${
          res.backup_snapshot || 'criado'
        }`,
      });
    } catch (err) {
      setBanner({
        type: 'err',
        text: err instanceof Error ? err.message : 'Erro de validação Pydantic no Perfil Mestre.',
      });
    } finally {
      setSaving(false);
    }
  };

  const handleSaveCriteria = async () => {
    if (!criteria) return;
    setSaving(true);
    setBanner(null);
    try {
      const res = await api.saveCriteria(criteria);
      setCriteria(res.criteria);
      setBanner({
        type: 'ok',
        text: `Critérios de busca salvos! Backup rotativo: ${res.backup_snapshot || 'criado'}`,
      });
    } catch (err) {
      setBanner({
        type: 'err',
        text: err instanceof Error ? err.message : 'Erro ao salvar critérios.',
      });
    } finally {
      setSaving(false);
    }
  };

  const handleSaveBlocked = async () => {
    if (!blocked) return;
    setSaving(true);
    setBanner(null);
    try {
      const res = await api.saveBlocked(blocked);
      setBlocked(res.blocked);
      setBanner({
        type: 'ok',
        text: `Lista de empresas bloqueadas salva! Backup: ${res.backup_snapshot || 'criado'}`,
      });
    } catch (err) {
      setBanner({
        type: 'err',
        text: err instanceof Error ? err.message : 'Erro ao salvar empresas bloqueadas.',
      });
    } finally {
      setSaving(false);
    }
  };

  const handleToggleSource = async (sourceId: string, nextEnabled: boolean) => {
    try {
      await api.toggleSource(sourceId, nextEnabled);
      setSources((prev) =>
        prev.map((s) => (s.id === sourceId ? { ...s, enabled: nextEnabled } : s))
      );
      setBanner({
        type: 'ok',
        text: `Fonte "${sourceId}" ${nextEnabled ? 'ativada' : 'desativada'}.`,
      });
    } catch (err) {
      setBanner({
        type: 'err',
        text: err instanceof Error ? err.message : 'Erro ao alternar conector.',
      });
    }
  };

  const handleSaveLlm = async (clearGemini = false, clearGroq = false) => {
    if (!llmConfig) return;
    setSaving(true);
    setBanner(null);
    try {
      const res = await api.saveLlmConfig({
        provider: llmConfig.provider,
        timeout_seconds: llmConfig.timeout_seconds,
        gemini_model: llmConfig.gemini_model,
        gemini_api_key: geminiKeyInput.trim() || undefined,
        clear_gemini_key: clearGemini,
        groq_model: llmConfig.groq_model,
        groq_api_key: groqKeyInput.trim() || undefined,
        clear_groq_key: clearGroq,
        ollama_base_url: llmConfig.ollama_base_url,
        ollama_model: llmConfig.ollama_model,
      });
      setLlmConfig(res.llm);
      setGeminiKeyInput('');
      setGroqKeyInput('');
      onRefreshHealth();
      setBanner({
        type: 'ok',
        text: `Configuração de IA Cirúrgica atualizada (provedor ativo: ${res.llm.provider}).`,
      });
    } catch (err) {
      setBanner({
        type: 'err',
        text: err instanceof Error ? err.message : 'Erro ao salvar provedor LLM.',
      });
    } finally {
      setSaving(false);
    }
  };

  if (!draftProfile) {
    return <div className="card" style={{ padding: 24 }}>Carregando Perfil Mestre...</div>;
  }

  const filteredSkills = draftProfile.skills.filter((sk) => {
    if (skillFilterVeracity && sk.veracity !== skillFilterVeracity) return false;
    if (
      skillSearch &&
      !sk.display_name.toLowerCase().includes(skillSearch.toLowerCase()) &&
      !sk.id.toLowerCase().includes(skillSearch.toLowerCase())
    ) {
      return false;
    }
    return true;
  });

  const activePreset = selectedPresetKey ? draftProfile.area_presets[selectedPresetKey] : null;

  return (
    <div className={styles.container}>
      {/* Sub-navigation Tabs */}
      <div className={styles.tabsNav}>
        {(
          [
            ['header', '1. Cabeçalho & Links'],
            ['experiences', '2. Experiências & Bullets'],
            ['skills', `3. Skills & Veracidade (${draftProfile.skills.length})`],
            ['education_courses', '4. Formação, Cursos & Projetos'],
            ['presets', `5. Presets de Área (${Object.keys(draftProfile.area_presets).length})`],
            ['criteria', '6. Critérios Geográficos & Busca'],
            ['blocked_sources', '7. Empresas Bloqueadas & Fontes'],
            ['llm', '8. Provedor IA Cirúrgica'],
          ] as Array<[SettingsTab, string]>
        ).map(([tabKey, label]) => (
          <button
            key={tabKey}
            type="button"
            className={`${styles.tabBtn} ${activeTab === tabKey ? styles.tabBtnActive : ''}`}
            onClick={() => setActiveTab(tabKey)}
          >
            {label}
          </button>
        ))}
      </div>

      {banner && (
        <div
          className={`bannerAlert ${banner.type === 'ok' ? 'badgeOk' : 'badgeDanger'}`}
          role="status"
        >
          <span>{banner.text}</span>
          <button type="button" className="btn btnSmall" onClick={() => setBanner(null)}>
            Fechar
          </button>
        </div>
      )}

      {/* TAB 1: HEADER & LINKS */}
      {activeTab === 'header' && (
        <div className={`card ${styles.sectionCard}`}>
          <div className={styles.sectionHeader}>
            <div>
              <h3>Dados de Cabeçalho &amp; Contato</h3>
              <span style={{ fontSize: 12.5, color: 'var(--text-secondary)' }}>
                Estes dados compõem exclusivamente o PDF local e são 100% removidos pelo
                PIIStripper antes de qualquer chamada LLM.
              </span>
            </div>
            <button
              type="button"
              className="btn btnPrimary"
              disabled={saving}
              onClick={handleSaveMasterProfile}
            >
              {saving ? 'Salvando...' : 'Salvar Perfil Mestre'}
            </button>
          </div>

          <div className={styles.grid3}>
            <div className="fieldGroup">
              <label className="fieldLabel">Nome Completo *</label>
              <input
                className="input"
                value={draftProfile.header.full_name}
                onChange={(e) =>
                  setDraftProfile({
                    ...draftProfile,
                    header: { ...draftProfile.header, full_name: e.target.value },
                  })
                }
              />
            </div>
            <div className="fieldGroup">
              <label className="fieldLabel">E-mail *</label>
              <input
                className="input"
                value={draftProfile.header.email}
                onChange={(e) =>
                  setDraftProfile({
                    ...draftProfile,
                    header: { ...draftProfile.header, email: e.target.value },
                  })
                }
              />
            </div>
            <div className="fieldGroup">
              <label className="fieldLabel">Telefone *</label>
              <input
                className="input"
                value={draftProfile.header.phone}
                onChange={(e) =>
                  setDraftProfile({
                    ...draftProfile,
                    header: { ...draftProfile.header, phone: e.target.value },
                  })
                }
              />
            </div>
          </div>

          <div className={styles.grid3}>
            <div className="fieldGroup">
              <label className="fieldLabel">Bairro (Opcional)</label>
              <input
                className="input"
                value={draftProfile.header.neighborhood ?? ''}
                onChange={(e) =>
                  setDraftProfile({
                    ...draftProfile,
                    header: {
                      ...draftProfile.header,
                      neighborhood: e.target.value || null,
                    },
                  })
                }
              />
            </div>
            <div className="fieldGroup">
              <label className="fieldLabel">Cidade *</label>
              <input
                className="input"
                value={draftProfile.header.city}
                onChange={(e) =>
                  setDraftProfile({
                    ...draftProfile,
                    header: { ...draftProfile.header, city: e.target.value },
                  })
                }
              />
            </div>
            <div className="fieldGroup">
              <label className="fieldLabel">Estado / UF *</label>
              <input
                className="input"
                value={draftProfile.header.state}
                onChange={(e) =>
                  setDraftProfile({
                    ...draftProfile,
                    header: { ...draftProfile.header, state: e.target.value },
                  })
                }
              />
            </div>
          </div>

          <div className={styles.stack}>
            <div className={styles.rowBetween}>
              <strong>Links Clicáveis no Cabeçalho A4 (LinkedIn / GitHub / Portfólio)</strong>
              <button
                type="button"
                className="btn btnSmall"
                onClick={() =>
                  setDraftProfile({
                    ...draftProfile,
                    header: {
                      ...draftProfile.header,
                      links: [
                        ...draftProfile.header.links,
                        {
                          label: 'Portfólio',
                          url: 'https://github.com/exemplo',
                          display_text: 'github.com/exemplo',
                        },
                      ],
                    },
                  })
                }
              >
                + Adicionar Link
              </button>
            </div>

            {draftProfile.header.links.map((lnk, idx) => (
              <div key={idx} className={styles.subCard}>
                <div className={styles.grid3}>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Rótulo</label>
                    <input
                      className="input"
                      value={lnk.label}
                      onChange={(e) => {
                        const next = [...draftProfile.header.links];
                        next[idx] = { ...lnk, label: e.target.value };
                        setDraftProfile({
                          ...draftProfile,
                          header: { ...draftProfile.header, links: next },
                        });
                      }}
                    />
                  </div>
                  <div className="fieldGroup">
                    <label className="fieldLabel">URL real clicável (`https://`)</label>
                    <input
                      className="input"
                      value={lnk.url}
                      onChange={(e) => {
                        const next = [...draftProfile.header.links];
                        next[idx] = { ...lnk, url: e.target.value };
                        setDraftProfile({
                          ...draftProfile,
                          header: { ...draftProfile.header, links: next },
                        });
                      }}
                    />
                  </div>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Texto visível impresso</label>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <input
                        className="input"
                        value={lnk.display_text}
                        onChange={(e) => {
                          const next = [...draftProfile.header.links];
                          next[idx] = { ...lnk, display_text: e.target.value };
                          setDraftProfile({
                            ...draftProfile,
                            header: { ...draftProfile.header, links: next },
                          });
                        }}
                      />
                      <button
                        type="button"
                        className="btn btnDanger btnSmall"
                        onClick={() => {
                          const next = draftProfile.header.links.filter((_, i) => i !== idx);
                          setDraftProfile({
                            ...draftProfile,
                            header: { ...draftProfile.header, links: next },
                          });
                        }}
                      >
                        Remover
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* TAB 2: EXPERIENCES & BULLETS */}
      {activeTab === 'experiences' && (
        <div className={`card ${styles.sectionCard}`}>
          <div className={styles.sectionHeader}>
            <div>
              <h3>Experiências Profissionais &amp; Bullets por Área/Skill</h3>
              <span style={{ fontSize: 12.5, color: 'var(--text-secondary)' }}>
                Substitui 100% a edição manual de `.docx`. Rótulos autodepreciativos (&ldquo;noções
                básicas&rdquo;) são bloqueados automaticamente pelo Pydantic.
              </span>
            </div>
            <button
              type="button"
              className="btn btnPrimary"
              disabled={saving}
              onClick={handleSaveMasterProfile}
            >
              {saving ? 'Salvando...' : 'Salvar Experiências'}
            </button>
          </div>

          {draftProfile.experiences.map((exp, expIdx) => (
            <div key={exp.id} className={styles.subCard}>
              <div className={styles.grid3}>
                <div className="fieldGroup">
                  <label className="fieldLabel">Cargo (pt-BR)</label>
                  <input
                    className="input"
                    value={exp.role}
                    onChange={(e) => {
                      const next = [...draftProfile.experiences];
                      next[expIdx] = { ...exp, role: e.target.value };
                      setDraftProfile({ ...draftProfile, experiences: next });
                    }}
                  />
                </div>
                <div className="fieldGroup">
                  <label className="fieldLabel">Empresa</label>
                  <input
                    className="input"
                    value={exp.company}
                    onChange={(e) => {
                      const next = [...draftProfile.experiences];
                      next[expIdx] = { ...exp, company: e.target.value };
                      setDraftProfile({ ...draftProfile, experiences: next });
                    }}
                  />
                </div>
                <div className="fieldGroup">
                  <label className="fieldLabel">Período</label>
                  <input
                    className="input"
                    value={exp.period_label}
                    onChange={(e) => {
                      const next = [...draftProfile.experiences];
                      next[expIdx] = { ...exp, period_label: e.target.value };
                      setDraftProfile({ ...draftProfile, experiences: next });
                    }}
                  />
                </div>
              </div>

              <div className={styles.rowBetween}>
                <strong>Bullets Factuais ({exp.bullets.length})</strong>
                <button
                  type="button"
                  className="btn btnSmall"
                  onClick={() => {
                    const newBullet: BulletItem = {
                      id: `b_custom_${Date.now().toString().slice(-5)}`,
                      text: 'Atuação estruturada em suporte técnico e documentação operacional de rotinas internas.',
                      areas: ['suporte'],
                      skill_tags: [],
                      keywords: [],
                      factual_source: 'profile_ui_editor',
                      translations: {},
                    };
                    const next = [...draftProfile.experiences];
                    next[expIdx] = { ...exp, bullets: [newBullet, ...exp.bullets] };
                    setDraftProfile({ ...draftProfile, experiences: next });
                  }}
                >
                  + Novo Bullet Factual
                </button>
              </div>

              <div className={styles.stack}>
                {exp.bullets.map((b, bIdx) => (
                  <div key={b.id} className="card" style={{ padding: 12 }}>
                    <div className={styles.rowBetween} style={{ marginBottom: 6 }}>
                      <span className="mono">{b.id}</span>
                      <span className="badge badgeNeutral">Fonte: {b.factual_source}</span>
                    </div>
                    <div className="fieldGroup" style={{ marginBottom: 8 }}>
                      <label className="fieldLabel">Texto (pt-BR)</label>
                      <textarea
                        rows={2}
                        className="textarea"
                        value={b.text}
                        onChange={(e) => {
                          const nextExps = [...draftProfile.experiences];
                          const nextBullets = [...exp.bullets];
                          nextBullets[bIdx] = { ...b, text: e.target.value };
                          nextExps[expIdx] = { ...exp, bullets: nextBullets };
                          setDraftProfile({ ...draftProfile, experiences: nextExps });
                        }}
                      />
                    </div>
                    <div className={styles.grid3}>
                      <div className="fieldGroup">
                        <label className="fieldLabel">Áreas (separadas por vírgula)</label>
                        <input
                          className="input"
                          value={b.areas.join(', ')}
                          onChange={(e) => {
                            const nextExps = [...draftProfile.experiences];
                            const nextBullets = [...exp.bullets];
                            nextBullets[bIdx] = {
                              ...b,
                              areas: splitCsv(e.target.value),
                            };
                            nextExps[expIdx] = { ...exp, bullets: nextBullets };
                            setDraftProfile({ ...draftProfile, experiences: nextExps });
                          }}
                        />
                      </div>
                      <div className="fieldGroup">
                        <label className="fieldLabel">Tags de Skills (`skill_tags`)</label>
                        <input
                          className="input"
                          value={b.skill_tags.join(', ')}
                          onChange={(e) => {
                            const nextExps = [...draftProfile.experiences];
                            const nextBullets = [...exp.bullets];
                            nextBullets[bIdx] = {
                              ...b,
                              skill_tags: splitCsv(e.target.value),
                            };
                            nextExps[expIdx] = { ...exp, bullets: nextBullets };
                            setDraftProfile({ ...draftProfile, experiences: nextExps });
                          }}
                        />
                      </div>
                      <div className="fieldGroup">
                        <label className="fieldLabel">Tradução `en-US` (p/ suporte-en)</label>
                        <input
                          className="input"
                          placeholder="Opcional"
                          value={b.translations['en-US'] ?? ''}
                          onChange={(e) => {
                            const nextExps = [...draftProfile.experiences];
                            const nextBullets = [...exp.bullets];
                            const nextTrans = { ...b.translations };
                            if (e.target.value.trim()) {
                              nextTrans['en-US'] = e.target.value;
                            } else {
                              delete nextTrans['en-US'];
                            }
                            nextBullets[bIdx] = { ...b, translations: nextTrans };
                            nextExps[expIdx] = { ...exp, bullets: nextBullets };
                            setDraftProfile({ ...draftProfile, experiences: nextExps });
                          }}
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* TAB 3: SKILLS CATALOG & VERACITY */}
      {activeTab === 'skills' && (
        <div className={`card ${styles.sectionCard}`}>
          <div className={styles.sectionHeader}>
            <div>
              <h3>Catálogo de Skills &amp; Governança de Veracidade</h3>
              <span style={{ fontSize: 12.5, color: 'var(--text-secondary)' }}>
                Apenas <code>confirmed_practice</code> e <code>academic_foundation</code> podem
                entrar no currículo. <code>preparable_gap</code> guarda o <code>study_plan</code>{' '}
                interno.
              </span>
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  const newSkill: SkillItem = {
                    id: `sk_${Date.now().toString().slice(-5)}`,
                    display_name: 'Nova Competência',
                    aliases: [],
                    areas: ['dev'],
                    veracity: 'confirmed_practice',
                    factual_source: 'profile_ui_editor',
                    translations: {},
                  };
                  setDraftProfile({
                    ...draftProfile,
                    skills: [newSkill, ...draftProfile.skills],
                  });
                }}
              >
                + Nova Skill
              </button>
              <button
                type="button"
                className="btn btnPrimary"
                disabled={saving}
                onClick={handleSaveMasterProfile}
              >
                {saving ? 'Salvando...' : 'Salvar Catálogo'}
              </button>
            </div>
          </div>

          <div className={styles.grid2}>
            <input
              className="input"
              placeholder="Buscar skill por nome ou ID..."
              value={skillSearch}
              onChange={(e) => setSkillSearch(e.target.value)}
            />
            <select
              className="select"
              value={skillFilterVeracity}
              onChange={(e) => setSkillFilterVeracity(e.target.value)}
            >
              <option value="">Todos os níveis de veracidade ({draftProfile.skills.length})</option>
              <option value="confirmed_practice">confirmed_practice (Prática Confirmada)</option>
              <option value="academic_foundation">academic_foundation (Base Acadêmica)</option>
              <option value="preparable_gap">preparable_gap (Lacuna c/ Plano de Estudo)</option>
              <option value="unconfirmed">unconfirmed (Não Confirmada / Bloqueada)</option>
            </select>
          </div>

          <div className={styles.stack}>
            {filteredSkills.slice(0, 60).map((sk) => {
              const realIdx = draftProfile.skills.findIndex((item) => item.id === sk.id);
              return (
                <div key={sk.id} className={styles.subCard}>
                  <div className={styles.grid3}>
                    <div className="fieldGroup">
                      <label className="fieldLabel">Nome Exibido (`{sk.id}`)</label>
                      <input
                        className="input"
                        value={sk.display_name}
                        onChange={(e) => {
                          const next = [...draftProfile.skills];
                          next[realIdx] = { ...sk, display_name: e.target.value };
                          setDraftProfile({ ...draftProfile, skills: next });
                        }}
                      />
                    </div>
                    <div className="fieldGroup">
                      <label className="fieldLabel">Estado de Veracidade</label>
                      <select
                        className="select"
                        value={sk.veracity}
                        onChange={(e) => {
                          const next = [...draftProfile.skills];
                          next[realIdx] = {
                            ...sk,
                            veracity: e.target.value as VeracityState,
                          };
                          setDraftProfile({ ...draftProfile, skills: next });
                        }}
                      >
                        <option value="confirmed_practice">confirmed_practice</option>
                        <option value="academic_foundation">academic_foundation</option>
                        <option value="preparable_gap">preparable_gap</option>
                        <option value="unconfirmed">unconfirmed</option>
                      </select>
                    </div>
                    <div className="fieldGroup">
                      <label className="fieldLabel">Aliases (vírgula)</label>
                      <input
                        className="input"
                        value={sk.aliases.join(', ')}
                        onChange={(e) => {
                          const next = [...draftProfile.skills];
                          next[realIdx] = { ...sk, aliases: splitCsv(e.target.value) };
                          setDraftProfile({ ...draftProfile, skills: next });
                        }}
                      />
                    </div>
                  </div>
                  {(sk.veracity === 'preparable_gap' || sk.study_plan) && (
                    <div className="fieldGroup">
                      <label className="fieldLabel">
                        Plano Interno de Estudo (`study_plan` — exibido na triagem, nunca no PDF)
                      </label>
                      <input
                        className="input"
                        value={sk.study_plan ?? ''}
                        onChange={(e) => {
                          const next = [...draftProfile.skills];
                          next[realIdx] = { ...sk, study_plan: e.target.value || null };
                          setDraftProfile({ ...draftProfile, skills: next });
                        }}
                      />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* TAB 4: EDUCATION, COURSES & PROJECTS */}
      {activeTab === 'education_courses' && (
        <div className={`card ${styles.sectionCard}`}>
          <div className={styles.sectionHeader}>
            <h3>Formação Acadêmica, Cursos Complementares &amp; Projetos</h3>
            <button
              type="button"
              className="btn btnPrimary"
              disabled={saving}
              onClick={handleSaveMasterProfile}
            >
              {saving ? 'Salvando...' : 'Salvar Formação & Projetos'}
            </button>
          </div>

          <div className={styles.stack}>
            <strong>Formação Acadêmica ({draftProfile.education.length})</strong>
            {draftProfile.education.map((edu, idx) => (
              <div key={edu.id} className={styles.subCard}>
                <div className={styles.grid3}>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Curso / Grau</label>
                    <input
                      className="input"
                      value={edu.degree}
                      onChange={(e) => {
                        const next = [...draftProfile.education];
                        next[idx] = { ...edu, degree: e.target.value };
                        setDraftProfile({ ...draftProfile, education: next });
                      }}
                    />
                  </div>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Instituição</label>
                    <input
                      className="input"
                      value={edu.institution}
                      onChange={(e) => {
                        const next = [...draftProfile.education];
                        next[idx] = { ...edu, institution: e.target.value };
                        setDraftProfile({ ...draftProfile, education: next });
                      }}
                    />
                  </div>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Período / Situação</label>
                    <input
                      className="input"
                      value={edu.period_label}
                      onChange={(e) => {
                        const next = [...draftProfile.education];
                        next[idx] = { ...edu, period_label: e.target.value };
                        setDraftProfile({ ...draftProfile, education: next });
                      }}
                    />
                  </div>
                </div>
              </div>
            ))}
          </div>

          <div className={styles.stack}>
            <strong>Cursos &amp; Vivências Práticas ({draftProfile.courses.length})</strong>
            {draftProfile.courses.map((c, idx) => (
              <div key={c.id} className={styles.subCard}>
                <div className={styles.grid3}>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Título</label>
                    <input
                      className="input"
                      value={c.title}
                      onChange={(e) => {
                        const next = [...draftProfile.courses];
                        next[idx] = { ...c, title: e.target.value };
                        setDraftProfile({ ...draftProfile, courses: next });
                      }}
                    />
                  </div>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Instituição</label>
                    <input
                      className="input"
                      value={c.institution}
                      onChange={(e) => {
                        const next = [...draftProfile.courses];
                        next[idx] = { ...c, institution: e.target.value };
                        setDraftProfile({ ...draftProfile, courses: next });
                      }}
                    />
                  </div>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Status (`in_progress_free` é bloqueado)</label>
                    <select
                      className="select"
                      value={c.status}
                      onChange={(e) => {
                        const next = [...draftProfile.courses];
                        next[idx] = {
                          ...c,
                          status: e.target.value as 'completed' | 'in_progress_free' | 'planned',
                        };
                        setDraftProfile({ ...draftProfile, courses: next });
                      }}
                    >
                      <option value="completed">completed (Permitido no PDF)</option>
                      <option value="in_progress_free">in_progress_free (Bloqueado no PDF)</option>
                      <option value="planned">planned (Planejado)</option>
                    </select>
                  </div>
                </div>
              </div>
            ))}
          </div>

          <div className={styles.stack}>
            <strong>Projetos ({draftProfile.projects.length})</strong>
            {draftProfile.projects.map((p, idx) => (
              <div key={p.id} className={styles.subCard}>
                <div className={styles.grid3}>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Nome do Projeto</label>
                    <input
                      className="input"
                      value={p.name}
                      onChange={(e) => {
                        const next = [...draftProfile.projects];
                        next[idx] = { ...p, name: e.target.value };
                        setDraftProfile({ ...draftProfile, projects: next });
                      }}
                    />
                  </div>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Status Editorial</label>
                    <select
                      className="select"
                      value={p.status}
                      onChange={(e) => {
                        const next = [...draftProfile.projects];
                        next[idx] = { ...p, status: e.target.value as 'approved' | 'reserved' };
                        setDraftProfile({ ...draftProfile, projects: next });
                      }}
                    >
                      <option value="approved">approved (Liberado p/ Presets)</option>
                      <option value="reserved">reserved (Confidencial / Bloqueado no PDF)</option>
                    </select>
                  </div>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Resumo</label>
                    <input
                      className="input"
                      value={p.summary ?? ''}
                      onChange={(e) => {
                        const next = [...draftProfile.projects];
                        next[idx] = { ...p, summary: e.target.value || null };
                        setDraftProfile({ ...draftProfile, projects: next });
                      }}
                    />
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* TAB 5: AREA PRESETS */}
      {activeTab === 'presets' && (
        <div className={`card ${styles.sectionCard}`}>
          <div className={styles.sectionHeader}>
            <div>
              <h3>Configuração dos Presets por Área</h3>
              <span style={{ fontSize: 12.5, color: 'var(--text-secondary)' }}>
                Suporta currículos em Português (<code>pt-BR</code>) e Inglês (<code>en-US</code>,
                como <code>suporte-en</code>).
              </span>
            </div>
            <button
              type="button"
              className="btn btnPrimary"
              disabled={saving}
              onClick={handleSaveMasterProfile}
            >
              {saving ? 'Salvando...' : 'Salvar Presets'}
            </button>
          </div>

          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {Object.values(draftProfile.area_presets).map((pr) => (
              <button
                key={pr.area_id}
                type="button"
                className={`btn ${selectedPresetKey === pr.area_id ? 'btnPrimary' : ''}`}
                onClick={() => setSelectedPresetKey(pr.area_id)}
              >
                {pr.display_name} ({pr.locale})
              </button>
            ))}
          </div>

          {activePreset && (
            <div className={styles.subCard}>
              <div className={styles.grid3}>
                <div className="fieldGroup">
                  <label className="fieldLabel">Nome do Preset</label>
                  <input
                    className="input"
                    value={activePreset.display_name}
                    onChange={(e) =>
                      setDraftProfile({
                        ...draftProfile,
                        area_presets: {
                          ...draftProfile.area_presets,
                          [activePreset.area_id]: {
                            ...activePreset,
                            display_name: e.target.value,
                          },
                        },
                      })
                    }
                  />
                </div>
                <div className="fieldGroup">
                  <label className="fieldLabel">Nome Dinâmico do PDF Exportado</label>
                  <input
                    className="input"
                    value={activePreset.output_pdf_filename}
                    onChange={(e) =>
                      setDraftProfile({
                        ...draftProfile,
                        area_presets: {
                          ...draftProfile.area_presets,
                          [activePreset.area_id]: {
                            ...activePreset,
                            output_pdf_filename: e.target.value,
                          },
                        },
                      })
                    }
                  />
                </div>
                <div className="fieldGroup">
                  <label className="fieldLabel">Orçamento Máximo de Páginas A4</label>
                  <select
                    className="select"
                    value={activePreset.max_pages}
                    onChange={(e) =>
                      setDraftProfile({
                        ...draftProfile,
                        area_presets: {
                          ...draftProfile.area_presets,
                          [activePreset.area_id]: {
                            ...activePreset,
                            max_pages: Number(e.target.value),
                          },
                        },
                      })
                    }
                  >
                    <option value={1}>1 Página A4 Estrita</option>
                    <option value={2}>Até 2 Páginas A4</option>
                  </select>
                </div>
              </div>

              <div className="fieldGroup">
                <label className="fieldLabel">Resumo Padrão da Área (`default_summary`)</label>
                <textarea
                  rows={3}
                  className="textarea"
                  value={activePreset.default_summary}
                  onChange={(e) =>
                    setDraftProfile({
                      ...draftProfile,
                      area_presets: {
                        ...draftProfile.area_presets,
                        [activePreset.area_id]: {
                          ...activePreset,
                          default_summary: e.target.value,
                        },
                      },
                    })
                  }
                />
              </div>

              <div className={styles.stack}>
                <strong>Grupos de Competências Impressos ({activePreset.skill_groups.length})</strong>
                {activePreset.skill_groups.map((grp, gIdx) => (
                  <div key={grp.id} className={styles.grid2}>
                    <input
                      className="input"
                      value={grp.label}
                      onChange={(e) => {
                        const nextGroups = [...activePreset.skill_groups];
                        nextGroups[gIdx] = { ...grp, label: e.target.value };
                        setDraftProfile({
                          ...draftProfile,
                          area_presets: {
                            ...draftProfile.area_presets,
                            [activePreset.area_id]: { ...activePreset, skill_groups: nextGroups },
                          },
                        });
                      }}
                    />
                    <input
                      className="input"
                      value={grp.items_text}
                      onChange={(e) => {
                        const nextGroups = [...activePreset.skill_groups];
                        nextGroups[gIdx] = { ...grp, items_text: e.target.value };
                        setDraftProfile({
                          ...draftProfile,
                          area_presets: {
                            ...draftProfile.area_presets,
                            [activePreset.area_id]: { ...activePreset, skill_groups: nextGroups },
                          },
                        });
                      }}
                    />
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* TAB 6: SEARCH & GEOGRAPHY CRITERIA */}
      {activeTab === 'criteria' && criteria && (
        <div className={`card ${styles.sectionCard}`}>
          <div className={styles.sectionHeader}>
            <h3>Critérios Geográficos, Horário e Regime de Contratação</h3>
            <button
              type="button"
              className="btn btnPrimary"
              disabled={saving}
              onClick={handleSaveCriteria}
            >
              {saving ? 'Salvando...' : 'Salvar Critérios de Triagem'}
            </button>
          </div>

          <div className={styles.stack}>
            <div className={styles.rowBetween}>
              <strong>Cidades Permitidas (Presencial / Híbrido)</strong>
              <button
                type="button"
                className="btn btnSmall"
                onClick={() =>
                  setCriteria({
                    ...criteria,
                    geography: {
                      ...criteria.geography,
                      allowed_cities: [
                        ...criteria.geography.allowed_cities,
                        {
                          slug: `cidade_${Date.now().toString().slice(-4)}`,
                          canonical_name: 'Nova Cidade',
                          state_code: 'SP',
                          aliases: ['nova cidade'],
                          neighborhoods: [],
                        },
                      ],
                    },
                  })
                }
              >
                + Adicionar Cidade
              </button>
            </div>

            {criteria.geography.allowed_cities.map((city, cIdx) => (
              <div key={city.slug} className={styles.subCard}>
                <div className={styles.grid3}>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Cidade</label>
                    <input
                      className="input"
                      value={city.canonical_name}
                      onChange={(e) => {
                        const next = [...criteria.geography.allowed_cities];
                        next[cIdx] = { ...city, canonical_name: e.target.value };
                        setCriteria({
                          ...criteria,
                          geography: { ...criteria.geography, allowed_cities: next },
                        });
                      }}
                    />
                  </div>
                  <div className="fieldGroup">
                    <label className="fieldLabel">UF (2 letras)</label>
                    <input
                      maxLength={2}
                      className="input"
                      value={city.state_code}
                      onChange={(e) => {
                        const next = [...criteria.geography.allowed_cities];
                        next[cIdx] = { ...city, state_code: e.target.value.toUpperCase() };
                        setCriteria({
                          ...criteria,
                          geography: { ...criteria.geography, allowed_cities: next },
                        });
                      }}
                    />
                  </div>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Aliases (vírgula)</label>
                    <input
                      className="input"
                      value={city.aliases.join(', ')}
                      onChange={(e) => {
                        const next = [...criteria.geography.allowed_cities];
                        next[cIdx] = { ...city, aliases: splitCsv(e.target.value) };
                        setCriteria({
                          ...criteria,
                          geography: { ...criteria.geography, allowed_cities: next },
                        });
                      }}
                    />
                  </div>
                </div>
              </div>
            ))}
          </div>

          <div className={styles.grid2}>
            <div className={styles.subCard}>
              <strong>Política de Remoto &amp; Região Metropolitana</strong>
              <label className={styles.checkRow}>
                <input
                  type="checkbox"
                  checked={criteria.geography.accept_remote_national}
                  onChange={(e) =>
                    setCriteria({
                      ...criteria,
                      geography: {
                        ...criteria.geography,
                        accept_remote_national: e.target.checked,
                      },
                    })
                  }
                />
                Aceitar vagas 100% Remotas Nacionais
              </label>
              <label className={styles.checkRow}>
                <input
                  type="checkbox"
                  checked={criteria.geography.reject_broader_metro_if_outside_allowed_cities}
                  onChange={(e) =>
                    setCriteria({
                      ...criteria,
                      geography: {
                        ...criteria.geography,
                        reject_broader_metro_if_outside_allowed_cities: e.target.checked,
                      },
                    })
                  }
                />
                Rejeitar cidades vizinhas fora da lista explícita
              </label>
            </div>

            <div className={styles.subCard}>
              <strong>Janela de Horário Permitida</strong>
              <div className={styles.grid3}>
                <div className="fieldGroup">
                  <label className="fieldLabel">Início (h)</label>
                  <input
                    type="number"
                    className="input"
                    value={criteria.schedule.earliest_hour}
                    onChange={(e) =>
                      setCriteria({
                        ...criteria,
                        schedule: {
                          ...criteria.schedule,
                          earliest_hour: Number(e.target.value),
                        },
                      })
                    }
                  />
                </div>
                <div className="fieldGroup">
                  <label className="fieldLabel">Fim Ideal (h)</label>
                  <input
                    type="number"
                    className="input"
                    value={criteria.schedule.latest_hour}
                    onChange={(e) =>
                      setCriteria({
                        ...criteria,
                        schedule: {
                          ...criteria.schedule,
                          latest_hour: Number(e.target.value),
                        },
                      })
                    }
                  />
                </div>
                <div className="fieldGroup">
                  <label className="fieldLabel">Tolerância (h)</label>
                  <input
                    type="number"
                    className="input"
                    value={criteria.schedule.soft_latest_hour}
                    onChange={(e) =>
                      setCriteria({
                        ...criteria,
                        schedule: {
                          ...criteria.schedule,
                          soft_latest_hour: Number(e.target.value),
                        },
                      })
                    }
                  />
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB 7: BLOCKED COMPANIES & SOURCES */}
      {activeTab === 'blocked_sources' && blocked && (
        <div className={`card ${styles.sectionCard}`}>
          <div className={styles.sectionHeader}>
            <div>
              <h3>Empresas Bloqueadas &amp; Fontes Públicas de Descoberta</h3>
              <span style={{ fontSize: 12.5, color: 'var(--text-secondary)' }}>
                Empresas bloqueadas nunca recebem candidatura, mas permanecem intactas no histórico
                profissional quando <code>preserve_in_work_history=true</code>.
              </span>
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                type="button"
                className="btn"
                onClick={() =>
                  setBlocked({
                    ...blocked,
                    companies: [
                      ...blocked.companies,
                      {
                        id: `blk_${Date.now().toString().slice(-4)}`,
                        display_name: 'Empresa Exemplo Ltda',
                        permanent: true,
                        domains: [],
                        aliases: ['empresa exemplo'],
                        confirmed_related_entities: [],
                        blocked_actions: ['resume_submission', 'automated_application'],
                        require_evidence_for_relations: true,
                        uncertain_relation_policy: 'hold_for_manual_review',
                        preserve_in_work_history: true,
                      },
                    ],
                  })
                }
              >
                + Bloquear Empresa
              </button>
              <button
                type="button"
                className="btn btnPrimary"
                disabled={saving}
                onClick={handleSaveBlocked}
              >
                Salvar Lista de Bloqueio
              </button>
            </div>
          </div>

          <div className={styles.stack}>
            {blocked.companies.map((comp, idx) => (
              <div key={comp.id} className={styles.subCard}>
                <div className={styles.grid3}>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Nome da Empresa</label>
                    <input
                      className="input"
                      value={comp.display_name}
                      onChange={(e) => {
                        const next = [...blocked.companies];
                        next[idx] = { ...comp, display_name: e.target.value };
                        setBlocked({ ...blocked, companies: next });
                      }}
                    />
                  </div>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Domínios (vírgula)</label>
                    <input
                      className="input"
                      value={comp.domains.join(', ')}
                      onChange={(e) => {
                        const next = [...blocked.companies];
                        next[idx] = { ...comp, domains: splitCsv(e.target.value) };
                        setBlocked({ ...blocked, companies: next });
                      }}
                    />
                  </div>
                  <div className="fieldGroup">
                    <label className="fieldLabel">Aliases (vírgula)</label>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <input
                        className="input"
                        value={comp.aliases.join(', ')}
                        onChange={(e) => {
                          const next = [...blocked.companies];
                          next[idx] = { ...comp, aliases: splitCsv(e.target.value) };
                          setBlocked({ ...blocked, companies: next });
                        }}
                      />
                      <button
                        type="button"
                        className="btn btnDanger btnSmall"
                        onClick={() =>
                          setBlocked({
                            ...blocked,
                            companies: blocked.companies.filter((_, i) => i !== idx),
                          })
                        }
                      >
                        Remover
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>

          <div className={styles.stack}>
            <strong>Conectores Públicos HTTPS (`sources.json`)</strong>
            {sources.map((src) => (
              <div key={src.id} className={styles.subCard}>
                <div className={styles.rowBetween}>
                  <div>
                    <strong>{src.id}</strong>{' '}
                    <span className="badge badgeInfo">{src.type}</span>{' '}
                    <span className="badge badgeNeutral">Prioridade: {src.priority}</span>
                    {src.notes && (
                      <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
                        {src.notes}
                      </div>
                    )}
                  </div>
                  <button
                    type="button"
                    className={`btn btnSmall ${src.enabled ? 'btnSuccess' : ''}`}
                    onClick={() => void handleToggleSource(src.id, !src.enabled)}
                  >
                    {src.enabled ? 'Ativa (Clique p/ Pausar)' : 'Pausada (Clique p/ Ativar)'}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* TAB 8: SURGICAL LLM PROVIDER */}
      {activeTab === 'llm' && llmConfig && (
        <div className={`card ${styles.sectionCard}`}>
          <div className={styles.sectionHeader}>
            <div>
              <h3>Configuração Segura do Provedor LLM (Modo IA Cirúrgica)</h3>
              <span style={{ fontSize: 12.5, color: 'var(--text-secondary)' }}>
                Chaves de API são gravadas apenas localmente em{' '}
                <code>user_data/config/llm_settings.json</code> (ignorado pelo Git) e jamais são
                retornadas via HTTP GET.
              </span>
            </div>
            <button
              type="button"
              className="btn btnPrimary"
              disabled={saving}
              onClick={() => void handleSaveLlm(false, false)}
            >
              {saving ? 'Salvando...' : 'Salvar Configuração LLM'}
            </button>
          </div>

          <div className={styles.grid2}>
            <div className="fieldGroup">
              <label className="fieldLabel">Provedor Ativo Padrão</label>
              <select
                className="select"
                value={llmConfig.provider}
                onChange={(e) =>
                  setLlmConfig({
                    ...llmConfig,
                    provider: e.target.value as 'none' | 'gemini' | 'groq' | 'ollama',
                  })
                }
              >
                <option value="none">Desativado (100% Determinístico Zero IA)</option>
                <option value="gemini">Google Gemini API</option>
                <option value="groq">Groq Cloud API</option>
                <option value="ollama">Ollama Local (Offline)</option>
              </select>
            </div>
            <div className="fieldGroup">
              <label className="fieldLabel">Timeout Máximo por Chamada (segundos)</label>
              <input
                type="number"
                min={3}
                max={120}
                className="input"
                value={llmConfig.timeout_seconds}
                onChange={(e) =>
                  setLlmConfig({ ...llmConfig, timeout_seconds: Number(e.target.value) || 15 })
                }
              />
            </div>
          </div>

          <div className={styles.subCard}>
            <div className={styles.rowBetween}>
              <strong>Google Gemini (`x-goog-api-key` via Header)</strong>
              <span
                className={`badge ${
                  llmConfig.gemini_key_configured ? 'badgeOk' : 'badgeNeutral'
                }`}
              >
                {llmConfig.gemini_key_configured ? 'Chave Configurada' : 'Sem Chave'}
              </span>
            </div>
            <div className={styles.grid2}>
              <div className="fieldGroup">
                <label className="fieldLabel">Modelo Gemini</label>
                <input
                  className="input"
                  value={llmConfig.gemini_model}
                  onChange={(e) => setLlmConfig({ ...llmConfig, gemini_model: e.target.value })}
                />
              </div>
              <div className="fieldGroup">
                <label className="fieldLabel">Nova API Key Gemini (deixe em branco p/ manter)</label>
                <div style={{ display: 'flex', gap: 6 }}>
                  <input
                    type="password"
                    className="input"
                    placeholder="AIza..."
                    value={geminiKeyInput}
                    onChange={(e) => setGeminiKeyInput(e.target.value)}
                  />
                  {llmConfig.gemini_key_configured && (
                    <button
                      type="button"
                      className="btn btnDanger btnSmall"
                      onClick={() => void handleSaveLlm(true, false)}
                    >
                      Limpar
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>

          <div className={styles.subCard}>
            <div className={styles.rowBetween}>
              <strong>Groq API (Compatível OpenAI JSON Object)</strong>
              <span
                className={`badge ${llmConfig.groq_key_configured ? 'badgeOk' : 'badgeNeutral'}`}
              >
                {llmConfig.groq_key_configured ? 'Chave Configurada' : 'Sem Chave'}
              </span>
            </div>
            <div className={styles.grid2}>
              <div className="fieldGroup">
                <label className="fieldLabel">Modelo Groq</label>
                <input
                  className="input"
                  value={llmConfig.groq_model}
                  onChange={(e) => setLlmConfig({ ...llmConfig, groq_model: e.target.value })}
                />
              </div>
              <div className="fieldGroup">
                <label className="fieldLabel">Nova API Key Groq (deixe em branco p/ manter)</label>
                <div style={{ display: 'flex', gap: 6 }}>
                  <input
                    type="password"
                    className="input"
                    placeholder="gsk_..."
                    value={groqKeyInput}
                    onChange={(e) => setGroqKeyInput(e.target.value)}
                  />
                  {llmConfig.groq_key_configured && (
                    <button
                      type="button"
                      className="btn btnDanger btnSmall"
                      onClick={() => void handleSaveLlm(false, true)}
                    >
                      Limpar
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>

          <div className={styles.subCard}>
            <strong>Ollama Local (100% na Máquina do Usuário)</strong>
            <div className={styles.grid2}>
              <div className="fieldGroup">
                <label className="fieldLabel">URL Base do Ollama</label>
                <input
                  className="input"
                  value={llmConfig.ollama_base_url}
                  onChange={(e) => setLlmConfig({ ...llmConfig, ollama_base_url: e.target.value })}
                />
              </div>
              <div className="fieldGroup">
                <label className="fieldLabel">Modelo Local</label>
                <input
                  className="input"
                  value={llmConfig.ollama_model}
                  onChange={(e) => setLlmConfig({ ...llmConfig, ollama_model: e.target.value })}
                />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
