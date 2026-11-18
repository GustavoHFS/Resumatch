import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type {
  DiffEntry,
  ExportPdfResponse,
  GuardWarning,
  JobListItem,
  MasterProfile,
  ResumeDocumentPayload,
  SectionKind,
} from '../types/api';
import styles from './ResumeStudioPage.module.css';

interface Props {
  profile: MasterProfile | null;
  initialJob: JobListItem | null;
  initialAreaId: string | null;
  onRefreshHealth: () => void;
}

const A4_PAGE_HEIGHT_PX = 1122.5; // 297mm @ 96 CSS DPI

export const ResumeStudioPage: React.FC<Props> = ({
  profile,
  initialJob,
  initialAreaId,
  onRefreshHealth,
}) => {
  const presetKeys = profile ? Object.keys(profile.area_presets) : [];
  const defaultArea =
    (initialAreaId && presetKeys.includes(initialAreaId) ? initialAreaId : presetKeys[0]) ||
    'suporte';

  const [areaId, setAreaId] = useState<string>(defaultArea);
  const [mode, setMode] = useState<'deterministic' | 'surgical_ai'>('deterministic');
  const [providerOverride, setProviderOverride] = useState<'' | 'gemini' | 'groq' | 'ollama'>('');

  const [jobsList, setJobsList] = useState<JobListItem[]>([]);
  const [linkedOpportunityId, setLinkedOpportunityId] = useState<string>(initialJob?.id ?? '');
  const [jobTitle, setJobTitle] = useState<string>(initialJob?.title ?? '');
  const [company, setCompany] = useState<string>(initialJob?.company ?? '');
  const [jobDescription, setJobDescription] = useState<string>('');

  const [payload, setPayload] = useState<ResumeDocumentPayload | null>(null);
  const [previewHtml, setPreviewHtml] = useState<string>('');
  const [composeMs, setComposeMs] = useState<number | null>(null);

  const [diffEntries, setDiffEntries] = useState<DiffEntry[]>([]);
  const [guardWarnings, setGuardWarnings] = useState<GuardWarning[]>([]);
  const [adaptMeta, setAdaptMeta] = useState<{
    provider: string;
    model: string;
    totalTokens: number;
  } | null>(null);

  const [autoFit, setAutoFit] = useState(true);
  const [zoom, setZoom] = useState<number>(0.74);
  const [measuredHeightPx, setMeasuredHeightPx] = useState<number>(A4_PAGE_HEIGHT_PX * 0.85);

  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [exportResult, setExportResult] = useState<ExportPdfResponse | null>(null);

  const iframeRef = useRef<HTMLIFrameElement | null>(null);

  useEffect(() => {
    api
      .getJobs({ limit: 80 })
      .then((res) => setJobsList(res.items))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (initialJob) {
      setLinkedOpportunityId(initialJob.id);
      setJobTitle(initialJob.title);
      setCompany(initialJob.company ?? '');
      if (initialAreaId && presetKeys.includes(initialAreaId)) {
        setAreaId(initialAreaId);
      }
    }
  }, [initialJob, initialAreaId, presetKeys]);

  const refreshHtmlFromPayload = useCallback(
    async (nextPayload: ResumeDocumentPayload, targetAreaId: string) => {
      const rendered = await api.renderResumeHtml({
        area_id: targetAreaId,
        opportunity_id: linkedOpportunityId || null,
        job_title: jobTitle || undefined,
        company: company || undefined,
        job_description: jobDescription,
        payload_override: nextPayload,
      });
      setPayload(rendered.payload);
      setPreviewHtml(rendered.html);
    },
    [linkedOpportunityId, jobTitle, company, jobDescription]
  );

  const runDeterministicCompose = useCallback(
    async (targetArea = areaId, sectionOrder?: SectionKind[]) => {
      if (!targetArea) return;
      setBusyAction('compose');
      setErrorMsg(null);
      try {
        const composed = await api.composeResume({
          area_id: targetArea,
          opportunity_id: linkedOpportunityId || null,
          job_title: jobTitle || undefined,
          company: company || undefined,
          job_description: jobDescription,
          section_order_override: sectionOrder,
        });
        setComposeMs(composed.elapsed_ms);
        setDiffEntries([]);
        setGuardWarnings([]);
        setAdaptMeta(null);
        await refreshHtmlFromPayload(composed.payload, targetArea);
      } catch (err) {
        setErrorMsg(err instanceof Error ? err.message : 'Erro ao montar currículo determinístico.');
      } finally {
        setBusyAction(null);
      }
    },
    [areaId, linkedOpportunityId, jobTitle, company, jobDescription, refreshHtmlFromPayload]
  );

  useEffect(() => {
    if (profile && areaId && profile.area_presets[areaId]) {
      void runDeterministicCompose(areaId);
    }
  }, [areaId, linkedOpportunityId]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleRunSurgicalAi = async () => {
    setBusyAction('adapt');
    setErrorMsg(null);
    try {
      const res = await api.adaptResumeSurgical({
        area_id: areaId,
        opportunity_id: linkedOpportunityId || null,
        job_title: jobTitle || undefined,
        company: company || undefined,
        job_description: jobDescription,
        section_order_override: payload?.section_order,
        provider_override: providerOverride || null,
      });
      setMode('surgical_ai');
      setDiffEntries(res.diff);
      setGuardWarnings(res.guard_warnings);
      setAdaptMeta({
        provider: res.provider,
        model: res.model,
        totalTokens: res.token_usage.total_tokens,
      });
      await refreshHtmlFromPayload(res.payload, areaId);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Falha na chamada cirúrgica de IA.');
    } finally {
      setBusyAction(null);
    }
  };

  const handleMoveSection = async (idx: number, delta: -1 | 1) => {
    if (!payload) return;
    const nextOrder = [...payload.section_order];
    const targetIdx = idx + delta;
    if (targetIdx < 0 || targetIdx >= nextOrder.length) return;
    const [moved] = nextOrder.splice(idx, 1);
    nextOrder.splice(targetIdx, 0, moved);
    const updated: ResumeDocumentPayload = { ...payload, section_order: nextOrder };
    await refreshHtmlFromPayload(updated, areaId);
  };

  const handleToggleBullet = async (
    expId: string,
    bulletId: string,
    bulletText: string,
    factualSource: string
  ) => {
    if (!payload) return;
    const nextExperiences = payload.experiences.map((exp) => {
      if (exp.id !== expId) return exp;
      const exists = exp.bullets.some((b) => b.id === bulletId);
      if (exists) {
        if (exp.bullets.length <= 1) return exp; // preserve at least 1 bullet
        return { ...exp, bullets: exp.bullets.filter((b) => b.id !== bulletId) };
      }
      return {
        ...exp,
        bullets: [
          ...exp.bullets,
          { id: bulletId, text: bulletText, score: 1, factual_source: factualSource },
        ],
      };
    });
    await refreshHtmlFromPayload({ ...payload, experiences: nextExperiences }, areaId);
  };

  const handleMoveActiveBullet = async (expId: string, bulletIdx: number, delta: -1 | 1) => {
    if (!payload) return;
    const nextExperiences = payload.experiences.map((exp) => {
      if (exp.id !== expId) return exp;
      const list = [...exp.bullets];
      const dest = bulletIdx + delta;
      if (dest < 0 || dest >= list.length) return exp;
      const [item] = list.splice(bulletIdx, 1);
      list.splice(dest, 0, item);
      return { ...exp, bullets: list };
    });
    await refreshHtmlFromPayload({ ...payload, experiences: nextExperiences }, areaId);
  };

  const handleToggleCompact = async (compact: boolean) => {
    if (!payload) return;
    await refreshHtmlFromPayload({ ...payload, compact_mode: compact }, areaId);
  };

  const handleExportPdf = async () => {
    if (!payload) return;
    setBusyAction('export');
    setErrorMsg(null);
    setExportResult(null);
    try {
      const exported = await api.exportResumePdf({
        area_id: areaId,
        opportunity_id: linkedOpportunityId || null,
        job_title: jobTitle || undefined,
        company: company || undefined,
        job_description: jobDescription,
        section_order_override: payload.section_order,
        generation_mode: mode,
        auto_fit: autoFit,
        payload_override: payload,
      });
      setExportResult(exported);
      setPayload(exported.payload);
      onRefreshHealth();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Erro ao exportar PDF A4.');
    } finally {
      setBusyAction(null);
    }
  };

  const measureIframeHeight = useCallback(() => {
    const doc = iframeRef.current?.contentDocument;
    if (!doc || !doc.body) return;
    const h = Math.max(doc.body.scrollHeight, doc.documentElement.scrollHeight);
    if (h > 100) {
      setMeasuredHeightPx(h);
    }
  }, []);

  const maxPages = payload?.max_pages ?? 1;
  const budgetPx = maxPages * A4_PAGE_HEIGHT_PX;
  const fillRatio = measuredHeightPx / budgetPx;
  const estimatedPages = Math.max(1, Math.ceil((measuredHeightPx - 12) / A4_PAGE_HEIGHT_PX));
  const isOverflow = measuredHeightPx > budgetPx + 14;
  const iframeDisplayHeight = Math.max(maxPages, estimatedPages) * A4_PAGE_HEIGHT_PX;

  const currentLocale = payload?.locale ?? profile?.area_presets[areaId]?.locale ?? 'pt-BR';

  return (
    <div className={styles.studioSplit}>
      {/* LEFT CONTROLS */}
      <div className={styles.leftControls}>
        {/* Area Preset Picker */}
        <div className={`card ${styles.panelCard}`}>
          <div className={styles.panelHeader}>
            <span className={styles.panelTitle}>1. Preset de Área &amp; Idioma</span>
            <span className="badge badgeInfo">
              Currículo: {currentLocale} • Limite: {maxPages} pág. A4
            </span>
          </div>
          <div className={styles.presetGrid}>
            {profile &&
              Object.values(profile.area_presets).map((preset) => (
                <button
                  key={preset.area_id}
                  type="button"
                  className={`${styles.presetBtn} ${
                    areaId === preset.area_id ? styles.presetBtnActive : ''
                  }`}
                  onClick={() => {
                    setAreaId(preset.area_id);
                    setExportResult(null);
                  }}
                >
                  <span>{preset.display_name}</span>
                  <span className="badge badgeNeutral">{preset.locale}</span>
                </button>
              ))}
          </div>
        </div>

        {/* Optional Linked Job / Context */}
        <div className={`card ${styles.panelCard}`}>
          <div className={styles.panelHeader}>
            <span className={styles.panelTitle}>2. Contexto da Vaga (Opcional)</span>
            <button
              type="button"
              className="btn btnSmall"
              disabled={busyAction !== null}
              onClick={() => runDeterministicCompose(areaId, payload?.section_order)}
            >
              Recalcular Seleção Determinística
            </button>
          </div>

          <div className="fieldGroup">
            <label className="fieldLabel">Vincular Vaga Salva no SQLite</label>
            <select
              className="select"
              value={linkedOpportunityId}
              onChange={(e) => {
                const id = e.target.value;
                setLinkedOpportunityId(id);
                const found = jobsList.find((j) => j.id === id);
                if (found) {
                  setJobTitle(found.title);
                  setCompany(found.company ?? '');
                }
              }}
            >
              <option value="">Sem vínculo fixo (apenas preset ou descrição colada abaixo)</option>
              {jobsList.map((j) => (
                <option key={j.id} value={j.id}>
                  [{j.area_id || 'geral'}] {j.title} — {j.company || 'Empresa omitida'}
                </option>
              ))}
            </select>
          </div>

          <div className={styles.twoCols}>
            <div className="fieldGroup">
              <label className="fieldLabel">Cargo Alvo</label>
              <input
                className="input"
                value={jobTitle}
                placeholder="Ex.: Analista de Suporte / Dev Jr"
                onChange={(e) => setJobTitle(e.target.value)}
              />
            </div>
            <div className="fieldGroup">
              <label className="fieldLabel">Empresa</label>
              <input
                className="input"
                value={company}
                placeholder="Opcional"
                onChange={(e) => setCompany(e.target.value)}
              />
            </div>
          </div>

          <div className="fieldGroup">
            <label className="fieldLabel">
              Descrição / Requisitos Adicionais para Pontuação de Bullets e Equivalências
            </label>
            <textarea
              rows={3}
              className="textarea"
              placeholder="Cole requisitos da vaga para priorizar bullets e equivalências lexicais auditadas..."
              value={jobDescription}
              onChange={(e) => setJobDescription(e.target.value)}
            />
          </div>
        </div>

        {/* Mode Toggle: Deterministic vs Surgical AI */}
        <div className={`card ${styles.panelCard}`}>
          <div className={styles.panelHeader}>
            <span className={styles.panelTitle}>3. Motor de Composição &amp; Exportação PDF</span>
            {composeMs !== null && (
              <span className="badge badgeOk">Determinístico: {composeMs} ms</span>
            )}
          </div>

          <div className={styles.modeToggleRow}>
            <button
              type="button"
              className={`${styles.modeButton} ${
                mode === 'deterministic' ? styles.modeButtonActive : ''
              }`}
              onClick={() => {
                setMode('deterministic');
                void runDeterministicCompose(areaId, payload?.section_order);
              }}
            >
              <span className={styles.modeTitle}>Modo Determinístico (Zero IA)</span>
              <span className={styles.modeSub}>
                Pontuação instantânea por área, tags e equivalências auditadas.
              </span>
            </button>

            <button
              type="button"
              className={`${styles.modeButton} ${
                mode === 'surgical_ai' ? styles.modeButtonActive : ''
              }`}
              onClick={() => setMode('surgical_ai')}
            >
              <span className={styles.modeTitle}>Modo IA Cirúrgica (1 Chamada)</span>
              <span className={styles.modeSub}>
                PIIStripper mascara instituições + VeracityGuard bloqueia alucinações.
              </span>
            </button>
          </div>

          {mode === 'surgical_ai' && (
            <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', flexWrap: 'wrap' }}>
              <div className="fieldGroup" style={{ flex: 1, minWidth: 180 }}>
                <label className="fieldLabel">Provedor LLM para Ajuste Fino</label>
                <select
                  className="select"
                  value={providerOverride}
                  onChange={(e) =>
                    setProviderOverride(e.target.value as '' | 'gemini' | 'groq' | 'ollama')
                  }
                >
                  <option value="">Usar provedor padrão configurado</option>
                  <option value="gemini">Google Gemini (Structured JSON)</option>
                  <option value="groq">Groq (Llama Structured)</option>
                  <option value="ollama">Ollama Local (127.0.0.1:11434)</option>
                </select>
              </div>
              <button
                type="button"
                className="btn btnPrimary"
                disabled={busyAction !== null}
                onClick={handleRunSurgicalAi}
              >
                {busyAction === 'adapt'
                  ? 'Executando IA Cirúrgica...'
                  : 'Executar Adaptação Cirúrgica'}
              </button>
            </div>
          )}

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              flexWrap: 'wrap',
              gap: 10,
              paddingTop: 6,
              borderTop: '1px solid var(--border-subtle)',
            }}
          >
            <div style={{ display: 'flex', gap: 14, alignItems: 'center' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5 }}>
                <input
                  type="checkbox"
                  checked={autoFit}
                  onChange={(e) => setAutoFit(e.target.checked)}
                />
                Auto-Fit A4 ({maxPages} pág.)
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5 }}>
                <input
                  type="checkbox"
                  checked={Boolean(payload?.compact_mode)}
                  onChange={(e) => void handleToggleCompact(e.target.checked)}
                />
                Tipografia Compacta
              </label>
            </div>

            <button
              type="button"
              className="btn btnPrimary"
              disabled={!payload || busyAction !== null}
              onClick={handleExportPdf}
            >
              {busyAction === 'export'
                ? 'Renderizando Chromium + Validando pypdf...'
                : 'Exportar PDF A4 Limpo'}
            </button>
          </div>

          {errorMsg && <div className="bannerAlert badgeDanger">{errorMsg}</div>}

          {exportResult && (
            <div className={`card ${styles.exportResultCard}`}>
              <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap' }}>
                <strong>✓ PDF A4 Validado pelo ATS Validator (`pypdf`)</strong>
                <span className="badge badgeOk">
                  {exportResult.ats_validation.page_count}/{exportResult.ats_validation.max_pages}{' '}
                  página(s)
                </span>
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                Caminho salvo: <code>user_data/{exportResult.export_rel_path}</code> •{' '}
                {(exportResult.pdf_size_bytes / 1024).toFixed(1)} KB •{' '}
                {exportResult.ats_validation.clickable_uris.length} link(s) clicáveis
              </div>
              <div className="mono" style={{ color: 'var(--text-muted)' }}>
                SHA-256: {exportResult.pdf_sha256.slice(0, 24)}…
              </div>
              <div>
                <a
                  className="btn btnSuccess btnSmall"
                  href={`/api/resumes/versions/${encodeURIComponent(exportResult.version_id)}/pdf`}
                  target="_blank"
                  rel="noreferrer"
                >
                  ⬇ Baixar Arquivo PDF ({exportResult.output_pdf_filename})
                </a>
              </div>
            </div>
          )}
        </div>

        {/* Diff Inspector & VeracityGuard Warnings */}
        {(diffEntries.length > 0 || guardWarnings.length > 0) && (
          <div className={`card ${styles.panelCard}`}>
            <div className={styles.panelHeader}>
              <span className={styles.panelTitle}>Inspetor de Diff &amp; VeracityGuard</span>
              {adaptMeta && (
                <span className="badge badgePurple">
                  {adaptMeta.provider}/{adaptMeta.model} • {adaptMeta.totalTokens} tokens
                </span>
              )}
            </div>

            {guardWarnings.map((w, idx) => (
              <div key={idx} className="bannerAlert badgeWarn">
                <span>
                  <strong>Fallback Granular [{w.block_id}]:</strong> {w.reason} — {w.detail}
                </span>
              </div>
            ))}

            <div className={styles.diffList}>
              {diffEntries.map((d) => (
                <div key={d.block_id} className={styles.diffBox}>
                  <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <strong className="mono">{d.block_id}</strong>
                    {d.reverted_to_deterministic ? (
                      <span className="badge badgeWarn">Revertido p/ Determinístico</span>
                    ) : d.changed ? (
                      <span className="badge badgeOk">Ajustado</span>
                    ) : (
                      <span className="badge badgeNeutral">Inalterado</span>
                    )}
                  </div>
                  {d.changed && (
                    <>
                      <div className={styles.diffBefore}>− {d.before}</div>
                      <div className={styles.diffAfter}>+ {d.after}</div>
                    </>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Section Ordering & Bullet Selector */}
        {payload && (
          <div className={`card ${styles.panelCard}`}>
            <span className={styles.panelTitle}>4. Ordem das Seções &amp; Curadoria de Bullets</span>

            <div className="fieldGroup">
              <label className="fieldLabel">Resumo Profissional Renderizado</label>
              <textarea
                rows={3}
                className="textarea"
                value={payload.summary.text}
                onChange={(e) => {
                  const next = {
                    ...payload,
                    summary: { ...payload.summary, text: e.target.value },
                  };
                  setPayload(next);
                }}
                onBlur={() => void refreshHtmlFromPayload(payload, areaId)}
              />
            </div>

            <div className="fieldGroup">
              <label className="fieldLabel">Ordenação Dinâmica de Seções</label>
              <div className={styles.reorderList}>
                {payload.section_order.map((sec, idx) => (
                  <div key={sec} className={styles.reorderItem}>
                    <span>
                      <strong>{idx + 1}.</strong> {payload.section_labels[sec] || sec} (
                      <code>{sec}</code>)
                    </span>
                    <div className={styles.bulletControls}>
                      <button
                        type="button"
                        className="btn btnSmall"
                        disabled={idx === 0}
                        onClick={() => void handleMoveSection(idx, -1)}
                      >
                        ↑
                      </button>
                      <button
                        type="button"
                        className="btn btnSmall"
                        disabled={idx === payload.section_order.length - 1}
                        onClick={() => void handleMoveSection(idx, 1)}
                      >
                        ↓
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Active & Available Bullets per Experience */}
            {profile?.experiences.map((masterExp) => {
              const renderedExp = payload.experiences.find((e) => e.id === masterExp.id);
              const activeBullets = renderedExp?.bullets ?? [];
              const activeIds = new Set(activeBullets.map((b) => b.id));

              return (
                <div key={masterExp.id} className="fieldGroup">
                  <label className="fieldLabel">
                    Bullets Ativos — {masterExp.role} ({activeBullets.length} selecionados)
                  </label>
                  <div className={styles.reorderList}>
                    {activeBullets.map((b, idx) => (
                      <div key={b.id} className={styles.bulletCard}>
                        <label style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
                          <input
                            type="checkbox"
                            checked
                            onChange={() =>
                              void handleToggleBullet(masterExp.id, b.id, b.text, b.factual_source)
                            }
                          />
                          <span>{b.text}</span>
                        </label>
                        <div className={styles.bulletControls}>
                          <button
                            type="button"
                            className="btn btnSmall"
                            disabled={idx === 0}
                            onClick={() => void handleMoveActiveBullet(masterExp.id, idx, -1)}
                          >
                            ↑
                          </button>
                          <button
                            type="button"
                            className="btn btnSmall"
                            disabled={idx === activeBullets.length - 1}
                            onClick={() => void handleMoveActiveBullet(masterExp.id, idx, 1)}
                          >
                            ↓
                          </button>
                        </div>
                      </div>
                    ))}

                    {masterExp.bullets
                      .filter((mb) => !activeIds.has(mb.id))
                      .slice(0, 14)
                      .map((mb) => {
                        const localizedText =
                          (currentLocale === 'en-US' && mb.translations['en-US']) || mb.text;
                        return (
                          <div
                            key={mb.id}
                            className={`${styles.bulletCard} ${styles.bulletCardInactive}`}
                          >
                            <label style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
                              <input
                                type="checkbox"
                                checked={false}
                                onChange={() =>
                                  void handleToggleBullet(
                                    masterExp.id,
                                    mb.id,
                                    localizedText,
                                    mb.factual_source
                                  )
                                }
                              />
                              <span>{localizedText}</span>
                            </label>
                            <span className="badge badgeNeutral">{mb.areas.join(', ')}</span>
                          </div>
                        );
                      })}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* RIGHT PANE: LIVE ISOLATED A4 SHEET + OVERFLOW METER */}
      <div className={styles.previewPane}>
        <div className={`card ${styles.meterCard}`}>
          <div className={styles.meterHeader}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <strong>Folha A4 ao Vivo (210 × 297 mm)</strong>
              <span
                className={`badge ${
                  isOverflow ? 'badgeDanger' : fillRatio > 0.92 ? 'badgeWarn' : 'badgeOk'
                }`}
              >
                {isOverflow
                  ? `⚠️ Estouro (~${estimatedPages} pág. / Limite ${maxPages})`
                  : `✓ ${Math.round(fillRatio * 100)}% do orçamento (${maxPages} pág.)`}
              </span>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span className="fieldLabel">Escala:</span>
              {[0.65, 0.74, 0.9, 1].map((z) => (
                <button
                  key={z}
                  type="button"
                  className={`btn btnSmall ${zoom === z ? 'btnPrimary' : ''}`}
                  onClick={() => setZoom(z)}
                >
                  {Math.round(z * 100)}%
                </button>
              ))}
            </div>
          </div>

          <div className={styles.meterTrack}>
            <div
              className={
                isOverflow
                  ? styles.meterFillDanger
                  : fillRatio > 0.92
                    ? styles.meterFillWarn
                    : styles.meterFillOk
              }
              style={{ width: `${Math.min(100, Math.round(fillRatio * 100))}%` }}
            />
          </div>
        </div>

        <div className={styles.paperStage}>
          <div
            className={styles.paperScaleWrapper}
            style={{
              width: `${794 * zoom}px`,
              height: `${iframeDisplayHeight * zoom}px`,
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
                title="Pré-visualização A4 Isolada"
                srcDoc={previewHtml}
                className={styles.a4Iframe}
                style={{ height: `${iframeDisplayHeight}px` }}
                onLoad={measureIframeHeight}
              />
              <div
                className={styles.pageBreakMarker}
                style={{ top: `${maxPages * A4_PAGE_HEIGHT_PX}px` }}
              >
                <span className={styles.pageBreakTag}>
                  Limite Máximo A4 ({maxPages} {maxPages === 1 ? 'página' : 'páginas'})
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
