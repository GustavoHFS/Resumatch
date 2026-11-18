import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type {
  ApplicationCard,
  ApplicationEventItem,
  KanbanStage,
  ResumeVersionRecord,
} from '../types/api';
import styles from './ApplicationsKanbanPage.module.css';

interface Props {
  onRefreshHealth: () => void;
}

const COLUMNS: Array<{ stage: KanbanStage; label: string; badgeCls: string }> = [
  { stage: 'descoberta', label: '1. Descoberta', badgeCls: 'badgeNeutral' },
  {
    stage: 'candidatura_a_preparar',
    label: '2. Candidatura a Preparar',
    badgeCls: 'badgeInfo',
  },
  { stage: 'curriculo_preparado', label: '3. Currículo Preparado', badgeCls: 'badgePurple' },
  {
    stage: 'aguardando_envio_manual',
    label: '4. Aguardando Envio Manual',
    badgeCls: 'badgeWarn',
  },
  { stage: 'enviada_manualmente', label: '5. Enviada Manualmente', badgeCls: 'badgeOk' },
  {
    stage: 'encerrada_ou_descartada',
    label: '6. Encerrada / Descartada',
    badgeCls: 'badgeDanger',
  },
];

const STAGE_TRANSITIONS: Record<KanbanStage, Array<{ to: KanbanStage; label: string }>> = {
  descoberta: [
    { to: 'candidatura_a_preparar', label: 'Preparar' },
    { to: 'curriculo_preparado', label: 'Vincular PDF' },
    { to: 'encerrada_ou_descartada', label: 'Descartar' },
  ],
  candidatura_a_preparar: [
    { to: 'curriculo_preparado', label: 'Vincular PDF' },
    { to: 'encerrada_ou_descartada', label: 'Descartar' },
  ],
  curriculo_preparado: [
    { to: 'aguardando_envio_manual', label: 'Pronta p/ Envio' },
    { to: 'enviada_manualmente', label: 'Confirmar Envio Manual' },
    { to: 'encerrada_ou_descartada', label: 'Encerrar' },
  ],
  aguardando_envio_manual: [
    { to: 'enviada_manualmente', label: 'Confirmar Envio Manual' },
    { to: 'curriculo_preparado', label: 'Voltar p/ Revisão' },
    { to: 'encerrada_ou_descartada', label: 'Encerrar' },
  ],
  enviada_manualmente: [],
  encerrada_ou_descartada: [{ to: 'candidatura_a_preparar', label: 'Reabrir' }],
};

export const ApplicationsKanbanPage: React.FC<Props> = ({ onRefreshHealth }) => {
  const [cards, setCards] = useState<ApplicationCard[]>([]);
  const [search, setSearch] = useState('');
  const [versions, setVersions] = useState<ResumeVersionRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [feedback, setFeedback] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  const [transitionModal, setTransitionModal] = useState<{
    card: ApplicationCard;
    targetStage: KanbanStage;
  } | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState<string>('');
  const [confirmManualCheck, setConfirmManualCheck] = useState<boolean>(false);
  const [transitionNotes, setTransitionNotes] = useState<string>('');
  const [submittingTransition, setSubmittingTransition] = useState(false);

  const [eventsModalCard, setEventsModalCard] = useState<ApplicationCard | null>(null);
  const [eventsList, setEventsList] = useState<ApplicationEventItem[]>([]);
  const [loadingEvents, setLoadingEvents] = useState(false);

  const loadBoard = useCallback(async () => {
    setLoading(true);
    try {
      const [appsRes, versRes] = await Promise.all([
        api.listApplications({ search: search || undefined }),
        api.listResumeVersions({ limit: 100 }),
      ]);
      setCards(appsRes.items);
      setVersions(versRes);
    } catch (err) {
      setFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Erro ao carregar Kanban.',
      });
    } finally {
      setLoading(false);
    }
  }, [search]);

  useEffect(() => {
    void loadBoard();
  }, [loadBoard]);

  const openTransitionDialog = (card: ApplicationCard, targetStage: KanbanStage) => {
    setTransitionModal({ card, targetStage });
    setSelectedVersionId(card.resume_version?.id ?? versions[0]?.id ?? '');
    setConfirmManualCheck(false);
    setTransitionNotes('');
  };

  const handleConfirmTransition = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!transitionModal) return;
    const { card, targetStage } = transitionModal;
    const needsVersion = [
      'curriculo_preparado',
      'aguardando_envio_manual',
      'enviada_manualmente',
    ].includes(targetStage);

    setSubmittingTransition(true);
    try {
      await api.transitionStage(card.opportunity_id, {
        stage: targetStage,
        resume_version_id: needsVersion ? selectedVersionId || null : undefined,
        confirm_manual_submission:
          targetStage === 'enviada_manualmente' ? confirmManualCheck : false,
        notes: transitionNotes.trim() || undefined,
      });
      setTransitionModal(null);
      onRefreshHealth();
      await loadBoard();
      setFeedback({
        type: 'ok',
        text: `Candidatura "${card.job.title}" movida para "${targetStage}".`,
      });
    } catch (err) {
      setFeedback({
        type: 'err',
        text: err instanceof Error ? err.message : 'Transição recusada pelas regras de governança.',
      });
    } finally {
      setSubmittingTransition(false);
    }
  };

  const openHistoryModal = async (card: ApplicationCard) => {
    setEventsModalCard(card);
    setLoadingEvents(true);
    try {
      const evs = await api.listApplicationEvents(card.opportunity_id);
      setEventsList(evs);
    } catch {
      setEventsList([]);
    } finally {
      setLoadingEvents(false);
    }
  };

  return (
    <div className={styles.container}>
      <div className={`card ${styles.headerBar}`}>
        <div>
          <h3>Kanban Auditado de Candidaturas</h3>
          <span style={{ fontSize: 12.5, color: 'var(--text-secondary)' }}>
            Governança ativa: nenhuma candidatura é marcada como enviada sem versão PDF imutável
            (SHA-256) e confirmação humana explícita.
          </span>
        </div>

        <div className={styles.filterRow}>
          <input
            className="input"
            style={{ width: 250 }}
            placeholder="Filtrar por cargo ou empresa..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button type="button" className="btn" onClick={() => void loadBoard()}>
            {loading ? 'Atualizando...' : 'Atualizar Quadro'}
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

      <div className={styles.boardScroll}>
        {COLUMNS.map((col) => {
          const colCards = cards.filter((c) => c.stage === col.stage);
          return (
            <div key={col.stage} className={styles.column}>
              <div className={styles.columnHeader}>
                <span className={styles.columnTitle}>{col.label}</span>
                <span className={`badge ${col.badgeCls}`}>{colCards.length}</span>
              </div>

              <div className={styles.cardsStack}>
                {colCards.map((card) => (
                  <div key={card.opportunity_id} className={styles.kanbanCard}>
                    <div>
                      <div className={styles.cardTitle}>{card.job.title}</div>
                      <div className={styles.cardCompany}>
                        {card.job.company || 'Empresa omitida'} • Nível {card.service_tier}
                      </div>
                    </div>

                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                      {card.job.area_id && (
                        <span className="badge badgeNeutral">{card.job.area_id}</span>
                      )}
                      {card.application_method && (
                        <span className="badge badgeInfo">Método {card.application_method}</span>
                      )}
                    </div>

                    {card.resume_version ? (
                      <div className={styles.versionBox}>
                        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                          <strong>Versão: {card.resume_version.area_id}</strong>
                          <span>{card.resume_version.generation_mode}</span>
                        </div>
                        {card.resume_version.pdf_sha256 && (
                          <span className="mono" style={{ color: 'var(--status-ok)' }}>
                            SHA-256: {card.resume_version.pdf_sha256.slice(0, 14)}…
                          </span>
                        )}
                        {card.resume_version.export_rel_path && (
                          <a
                            href={`/api/resumes/versions/${encodeURIComponent(
                              card.resume_version.id
                            )}/pdf`}
                            target="_blank"
                            rel="noreferrer"
                          >
                            ⬇ Abrir PDF Vinculado
                          </a>
                        )}
                      </div>
                    ) : (
                      <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                        Nenhum PDF vinculado ainda
                      </span>
                    )}

                    {card.user_confirmed_at && (
                      <span className="badge badgeOk">
                        ✓ Envio confirmado em{' '}
                        {new Date(card.user_confirmed_at).toLocaleDateString('pt-BR')}
                      </span>
                    )}

                    <div className={styles.cardActions}>
                      {STAGE_TRANSITIONS[card.stage]?.map((tr) => (
                        <button
                          key={tr.to}
                          type="button"
                          className={`btn btnSmall ${
                            tr.to === 'enviada_manualmente' ? 'btnPrimary' : ''
                          }`}
                          onClick={() => openTransitionDialog(card, tr.to)}
                        >
                          {tr.label}
                        </button>
                      ))}
                      <button
                        type="button"
                        className="btn btnSmall"
                        onClick={() => void openHistoryModal(card)}
                      >
                        Histórico
                      </button>
                      <a
                        href={card.job.original_url}
                        target="_blank"
                        rel="noreferrer"
                        className="btn btnSmall"
                      >
                        Vaga ↗
                      </a>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      {/* Stage Transition / Manual Submission Modal */}
      {transitionModal && (
        <div className="modalBackdrop" onClick={() => setTransitionModal(null)}>
          <form
            className="modalDialog"
            onClick={(e) => e.stopPropagation()}
            onSubmit={handleConfirmTransition}
          >
            <div className="modalHeader">
              <h3>
                Mover Estágio → <code>{transitionModal.targetStage}</code>
              </h3>
              <button
                type="button"
                className="btn btnSmall"
                onClick={() => setTransitionModal(null)}
              >
                ✕
              </button>
            </div>

            <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
              Vaga: <strong>{transitionModal.card.job.title}</strong> (
              {transitionModal.card.job.company || 'Empresa omitida'})
            </p>

            {[
              'curriculo_preparado',
              'aguardando_envio_manual',
              'enviada_manualmente',
            ].includes(transitionModal.targetStage) && (
              <div className="fieldGroup">
                <label className="fieldLabel">
                  Versão de Currículo PDF Auditada (`resume_version_id`) *
                </label>
                <select
                  required
                  className="select"
                  value={selectedVersionId}
                  onChange={(e) => setSelectedVersionId(e.target.value)}
                >
                  <option value="">Selecione uma versão exportada...</option>
                  {versions.map((v) => (
                    <option key={v.id} value={v.id}>
                      [{v.area_id}] {v.job_title || 'Preset Geral'} — SHA:{' '}
                      {v.pdf_sha256 ? v.pdf_sha256.slice(0, 10) : 'legado'} (
                      {new Date(v.created_at).toLocaleDateString('pt-BR')})
                    </option>
                  ))}
                </select>
              </div>
            )}

            {transitionModal.targetStage === 'enviada_manualmente' && (
              <label
                className="bannerAlert badgeWarn"
                style={{ justifyContent: 'flex-start', cursor: 'pointer' }}
              >
                <input
                  required
                  type="checkbox"
                  checked={confirmManualCheck}
                  onChange={(e) => setConfirmManualCheck(e.target.checked)}
                />
                <span>
                  Confirmo que submeti manualmente esta candidatura no canal/portal oficial usando o
                  PDF selecionado acima (`confirm_manual_submission=true`).
                </span>
              </label>
            )}

            <div className="fieldGroup">
              <label className="fieldLabel">Observações do Evento (Opcional)</label>
              <textarea
                rows={3}
                className="textarea"
                placeholder="Ex.: Enviado por e-mail corporativo com assunto conforme edital..."
                value={transitionNotes}
                onChange={(e) => setTransitionNotes(e.target.value)}
              />
            </div>

            <div className="modalActions">
              <button type="button" className="btn" onClick={() => setTransitionModal(null)}>
                Cancelar
              </button>
              <button
                type="submit"
                className="btn btnPrimary"
                disabled={
                  submittingTransition ||
                  (transitionModal.targetStage === 'enviada_manualmente' && !confirmManualCheck)
                }
              >
                {submittingTransition ? 'Registrando...' : 'Confirmar Transição'}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Events Audit Timeline Modal */}
      {eventsModalCard && (
        <div className="modalBackdrop" onClick={() => setEventsModalCard(null)}>
          <div className="modalDialog" onClick={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <h3>Trilha de Auditoria — {eventsModalCard.job.title}</h3>
              <button
                type="button"
                className="btn btnSmall"
                onClick={() => setEventsModalCard(null)}
              >
                ✕
              </button>
            </div>

            {loadingEvents ? (
              <p>Carregando eventos...</p>
            ) : eventsList.length === 0 ? (
              <p style={{ color: 'var(--text-muted)' }}>Nenhum evento registrado.</p>
            ) : (
              <div className={styles.timelineList}>
                {eventsList.map((ev) => (
                  <div key={ev.id} className={styles.timelineItem}>
                    <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                      <span className="badge badgeInfo">{ev.stage}</span>
                      <span className="mono" style={{ color: 'var(--text-muted)' }}>
                        {new Date(ev.created_at).toLocaleString('pt-BR')}
                      </span>
                    </div>
                    <div>{ev.notes || 'Sem observações adicionais'}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};
