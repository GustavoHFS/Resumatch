import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import { GoogleConnectCard } from './GoogleConnectCard';
import { IconChevronDown, IconClose, IconPlus, IconTrash } from './Icons';
import type {
  BlockedCompaniesConfig,
  JobSourceItem,
  LlmConfigResponse,
  SchedulerStatus,
  SearchCriteria,
} from '../types/api';
import styles from './SettingsDrawer.module.css';

interface Props {
  open: boolean;
  onClose: () => void;
  onLlmUpdated: () => void;
}

const CATEGORY_ORDER = [
  'Grandes Portais',
  'Polo Tech Recife',
  'Tech & Remoto',
  'Regionais de Pernambuco',
  'Geral',
];

const DEFAULT_LLM: LlmConfigResponse = {
  connected: false,
  model: 'gemini-2.5-flash',
};

const DEFAULT_SCHEDULER: SchedulerStatus = {
  enabled: false,
  interval_minutes: 360,
  max_requests_budget: 80,
};

const DEFAULT_BLOCKED: BlockedCompaniesConfig = {
  schema_version: '1.0',
  companies: [],
};

export const SettingsDrawer: React.FC<Props> = ({ open, onClose, onLlmUpdated }) => {
  const [llm, setLlm] = useState<LlmConfigResponse>(DEFAULT_LLM);
  const [scheduler, setScheduler] = useState<SchedulerStatus>(DEFAULT_SCHEDULER);
  const [criteria, setCriteria] = useState<SearchCriteria | null>(null);
  const [newCityName, setNewCityName] = useState('');
  const [newCityUf, setNewCityUf] = useState('');

  const [sources, setSources] = useState<JobSourceItem[]>([]);
  const [openCategories, setOpenCategories] = useState<Record<string, boolean>>({
    'Grandes Portais': true,
    'Polo Tech Recife': true,
    'Tech & Remoto': false,
    'Regionais de Pernambuco': false,
  });

  const [blocked, setBlocked] = useState<BlockedCompaniesConfig>(DEFAULT_BLOCKED);
  const [newBlockedName, setNewBlockedName] = useState('');

  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [llmRes, schedRes, critRes, srcRes, blkRes] = await Promise.allSettled([
        api.getLlmConfig(),
        api.getScheduler(),
        api.getCriteria(),
        api.getSources(),
        api.getBlocked(),
      ]);

      const failedSections: string[] = [];

      if (llmRes.status === 'fulfilled') {
        setLlm(llmRes.value);
      } else {
        failedSections.push('Conexão Google');
      }

      if (schedRes.status === 'fulfilled') {
        setScheduler(schedRes.value);
      } else {
        failedSections.push('Agendamento');
      }

      if (critRes.status === 'fulfilled') {
        setCriteria(critRes.value);
      } else {
        failedSections.push('Critérios');
      }

      if (srcRes.status === 'fulfilled') {
        setSources(Array.isArray(srcRes.value.sources) ? srcRes.value.sources : []);
      } else {
        failedSections.push('Fontes de Vagas');
      }

      if (blkRes.status === 'fulfilled') {
        setBlocked(blkRes.value);
      } else {
        failedSections.push('Empresas Bloqueadas');
      }

      if (failedSections.length > 0) {
        setNotice(`Algumas seções não puderam ser lidas (${failedSections.join(', ')}).`);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setNotice(null);
    void loadAll();
  }, [open, loadAll]);

  if (!open) return null;

  const handleLlmChanged = () => {
    api
      .getLlmConfig()
      .then(setLlm)
      .catch(() => {});
    onLlmUpdated();
  };

  const handleToggleScheduler = async (enabled: boolean, overrideMinutes?: number) => {
    const clampedMinutes = Math.min(
      10080,
      Math.max(15, Math.round(overrideMinutes ?? scheduler.interval_minutes ?? 60))
    );
    try {
      const updated = await api.updateScheduler({
        enabled,
        interval_minutes: clampedMinutes,
      });
      setScheduler(updated);
      setNotice(
        overrideMinutes !== undefined
          ? `Intervalo salvo (${updated.interval_minutes} min).`
          : enabled
            ? 'Busca automática ativada.'
            : 'Busca automática desativada.'
      );
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Erro ao atualizar agendamento.');
    }
  };

  const handleSaveCriteria = async (nextCriteria: SearchCriteria) => {
    try {
      const res = await api.saveCriteria(nextCriteria);
      setCriteria(res.criteria);
      setNotice('Critérios de busca salvos.');
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Erro ao salvar critérios.');
    }
  };

  const handleAddCity = async () => {
    if (!criteria || !newCityName.trim() || newCityUf.trim().length !== 2) return;
    const name = newCityName.trim();
    const uf = newCityUf.trim().toUpperCase();
    const slug = name
      .toLowerCase()
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_|_$/g, '');
    const next: SearchCriteria = {
      ...criteria,
      geography: {
        ...criteria.geography,
        allowed_cities: [
          ...(criteria.geography?.allowed_cities ?? []),
          {
            slug: slug || `cidade_${Date.now()}`,
            canonical_name: name,
            state_code: uf,
            aliases: [name.toLowerCase()],
            neighborhoods: [],
          },
        ],
      },
    };
    setNewCityName('');
    setNewCityUf('');
    await handleSaveCriteria(next);
  };

  const handleRemoveCity = async (idx: number) => {
    const currentCities = criteria?.geography?.allowed_cities ?? [];
    if (!criteria || currentCities.length <= 1) return;
    const next: SearchCriteria = {
      ...criteria,
      geography: {
        ...criteria.geography,
        allowed_cities: currentCities.filter((_, i) => i !== idx),
      },
    };
    await handleSaveCriteria(next);
  };

  const handleToggleSource = async (sourceId: string, nextEnabled: boolean) => {
    try {
      await api.toggleSource(sourceId, nextEnabled);
      setSources((prev) =>
        prev.map((s) => (s.id === sourceId ? { ...s, enabled: nextEnabled } : s))
      );
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Erro ao atualizar fonte.');
    }
  };

  const handleAddBlocked = async () => {
    if (!newBlockedName.trim()) return;
    const name = newBlockedName.trim();
    const next: BlockedCompaniesConfig = {
      ...blocked,
      companies: [
        ...(blocked.companies ?? []),
        {
          id: `blk_${Date.now().toString().slice(-5)}`,
          display_name: name,
          permanent: true,
          domains: [],
          aliases: [name.toLowerCase()],
          confirmed_related_entities: [],
          blocked_actions: ['resume_submission', 'automated_application'],
          require_evidence_for_relations: true,
          uncertain_relation_policy: 'hold_for_manual_review',
          preserve_in_work_history: true,
        },
      ],
    };
    try {
      const res = await api.saveBlocked(next);
      setBlocked(res.blocked);
      setNewBlockedName('');
      setNotice('Empresa adicionada à lista de bloqueio.');
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Erro ao salvar empresa bloqueada.');
    }
  };

  const handleRemoveBlocked = async (idx: number) => {
    const next: BlockedCompaniesConfig = {
      ...blocked,
      companies: (blocked.companies ?? []).filter((_, i) => i !== idx),
    };
    try {
      const res = await api.saveBlocked(next);
      setBlocked(res.blocked);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Erro ao remover empresa bloqueada.');
    }
  };

  const groupedSources = sources.reduce<Record<string, JobSourceItem[]>>((acc, item) => {
    const cat = item.category?.trim() || 'Geral';
    if (!acc[cat]) acc[cat] = [];
    acc[cat].push(item);
    return acc;
  }, {});

  const sortedCategories = Object.keys(groupedSources).sort((a, b) => {
    const ia = CATEGORY_ORDER.indexOf(a);
    const ib = CATEGORY_ORDER.indexOf(b);
    if (ia !== -1 && ib !== -1) return ia - ib;
    if (ia !== -1) return -1;
    if (ib !== -1) return 1;
    return a.localeCompare(b, 'pt-BR');
  });

  const toggleCategory = (cat: string) => {
    setOpenCategories((prev) => ({ ...prev, [cat]: !prev[cat] }));
  };

  const allowedCities = criteria?.geography?.allowed_cities ?? [];
  const blockedCompanies = blocked.companies ?? [];

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <aside className={styles.drawer} onClick={(e) => e.stopPropagation()}>
        <div className={styles.drawerHeader}>
          <h2 className={styles.drawerTitle}>Configurações</h2>
          <div className={styles.inlineAdd}>
            <button
              type="button"
              className="btnSubtle btnSmall"
              disabled={loading}
              onClick={() => void loadAll()}
            >
              {loading ? 'Atualizando...' : 'Recarregar'}
            </button>
            <button
              type="button"
              className="iconBtn"
              onClick={onClose}
              aria-label="Fechar configurações"
            >
              <IconClose size={17} />
            </button>
          </div>
        </div>

        <div className={styles.drawerBody}>
          {notice && (
            <div className="noticeBar">
              <span>{notice}</span>
              <button type="button" className="iconBtn" onClick={() => setNotice(null)}>
                <IconClose size={14} />
              </button>
            </div>
          )}

          {/* 1. CONEXÃO GOOGLE IA — SEMPRE VISÍVEL */}
          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>Conexão Google IA</h3>
            <GoogleConnectCard
              connected={Boolean(llm.connected)}
              onStatusChanged={handleLlmChanged}
              compact
            />
          </section>

          {/* 2. FONTES DE VAGAS — SEMPRE VISÍVEL COM ACCORDIONS DAS 4 CATEGORIAS */}
          <section className={styles.section}>
            <div className={styles.rowBetween}>
              <h3 className={styles.sectionTitle}>Fontes de Vagas</h3>
              <span className={styles.metaCount}>
                {sources.filter((s) => s.enabled).length}/{sources.length} ativas
              </span>
            </div>

            {sources.length === 0 ? (
              <div className={styles.cardBox}>
                <span className={styles.metaCount}>
                  {loading
                    ? 'Carregando catálogo de portais...'
                    : 'Nenhuma fonte carregada. Clique em Recarregar no topo.'}
                </span>
              </div>
            ) : (
              <div className={styles.categoryStack}>
                {sortedCategories.map((cat) => {
                  const items = groupedSources[cat];
                  const activeCount = items.filter((s) => s.enabled).length;
                  const isOpen = Boolean(openCategories[cat]);

                  return (
                    <div key={cat} className={styles.categoryAccordion}>
                      <button
                        type="button"
                        className={styles.categoryHeader}
                        onClick={() => toggleCategory(cat)}
                        aria-expanded={isOpen}
                      >
                        <div className={styles.categoryTitleWrap}>
                          <span className={styles.categoryName}>{cat}</span>
                          <span className={styles.metaCount}>
                            {activeCount}/{items.length}
                          </span>
                        </div>
                        <span className={`chevron ${isOpen ? 'chevronOpen' : ''}`}>
                          <IconChevronDown size={15} />
                        </span>
                      </button>

                      <div className={`accordionCollapse ${isOpen ? 'accordionCollapseOpen' : ''}`}>
                        <div className="accordionCollapseInner">
                          <div className={styles.categoryBody}>
                            {items.map((src) => (
                              <div key={src.id} className={styles.sourceRow}>
                                <span className={styles.sourceName}>
                                  {src.display_name || src.id}
                                </span>
                                <button
                                  type="button"
                                  role="switch"
                                  aria-checked={src.enabled}
                                  aria-label={`Alternar fonte ${src.display_name || src.id}`}
                                  className="switchBtn"
                                  onClick={() => void handleToggleSource(src.id, !src.enabled)}
                                >
                                  <span className="switchThumb" />
                                </button>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* 3. CRITÉRIOS DE BUSCA — SEMPRE VISÍVEL */}
          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>Critérios de Busca</h3>
            <div className={styles.cardBox}>
              <div className={styles.rowBetween}>
                <span>Aceitar vagas remotas no Brasil</span>
                <button
                  type="button"
                  role="switch"
                  disabled={!criteria}
                  aria-checked={Boolean(criteria?.geography?.accept_remote_national)}
                  aria-label="Aceitar vagas remotas no Brasil"
                  className="switchBtn"
                  onClick={() => {
                    if (!criteria) return;
                    void handleSaveCriteria({
                      ...criteria,
                      geography: {
                        ...criteria.geography,
                        accept_remote_national: !criteria.geography.accept_remote_national,
                      },
                    });
                  }}
                >
                  <span className="switchThumb" />
                </button>
              </div>
            </div>

            {allowedCities.map((c, idx) => (
              <div key={c.slug} className={styles.listRow}>
                <span>
                  {c.canonical_name} — {c.state_code}
                </span>
                {allowedCities.length > 1 && (
                  <button
                    type="button"
                    className="iconBtn"
                    onClick={() => void handleRemoveCity(idx)}
                    title="Remover cidade"
                  >
                    <IconTrash size={14} />
                  </button>
                )}
              </div>
            ))}

            <div className={styles.inlineAdd}>
              <input
                className="input"
                placeholder="Nova cidade"
                disabled={!criteria}
                value={newCityName}
                onChange={(e) => setNewCityName(e.target.value)}
              />
              <input
                className="input"
                style={{ width: 68 }}
                maxLength={2}
                placeholder="UF"
                disabled={!criteria}
                value={newCityUf}
                onChange={(e) => setNewCityUf(e.target.value.toUpperCase())}
              />
              <button
                type="button"
                className="btnSubtle"
                disabled={!criteria}
                onClick={() => void handleAddCity()}
                title="Adicionar cidade"
              >
                <IconPlus size={15} />
              </button>
            </div>
          </section>

          {/* 4. AGENDAMENTO — SEMPRE VISÍVEL */}
          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>Agendamento</h3>
            <div className={styles.cardBox}>
              <div className={styles.rowBetween}>
                <label className="label">Intervalo em minutos</label>
                <div className={styles.inlineAdd}>
                  <input
                    type="number"
                    min={15}
                    max={1440}
                    className="input"
                    style={{ width: 90 }}
                    value={scheduler.interval_minutes}
                    onChange={(e) =>
                      setScheduler({
                        ...scheduler,
                        interval_minutes: Number(e.target.value) || 15,
                      })
                    }
                    onBlur={() =>
                      void handleToggleScheduler(scheduler.enabled, scheduler.interval_minutes)
                    }
                  />
                  <button
                    type="button"
                    role="switch"
                    aria-checked={scheduler.enabled}
                    aria-label="Alternar busca automática"
                    className="switchBtn"
                    onClick={() => void handleToggleScheduler(!scheduler.enabled)}
                  >
                    <span className="switchThumb" />
                  </button>
                </div>
              </div>
            </div>
          </section>

          {/* 5. EMPRESAS BLOQUEADAS — SEMPRE VISÍVEL */}
          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>Empresas Bloqueadas</h3>
            {blockedCompanies.map((comp, idx) => (
              <div key={comp.id} className={styles.listRow}>
                <span>{comp.display_name}</span>
                <button
                  type="button"
                  className="iconBtn"
                  title="Desbloquear empresa"
                  onClick={() => void handleRemoveBlocked(idx)}
                >
                  <IconTrash size={14} />
                </button>
              </div>
            ))}

            <div className={styles.inlineAdd}>
              <input
                className="input"
                placeholder="Nome da empresa para ignorar..."
                value={newBlockedName}
                onChange={(e) => setNewBlockedName(e.target.value)}
              />
              <button
                type="button"
                className="btnSubtle"
                onClick={() => void handleAddBlocked()}
                title="Bloquear empresa"
              >
                <IconPlus size={15} />
              </button>
            </div>
          </section>
          <footer style={{ fontSize: 11, opacity: 0.65, marginTop: 24 }}>
            Resumatch • Desenvolvido por Gustavo Henrique Francisco da Silva
          </footer>
        </div>
      </aside>
    </div>
  );
};
