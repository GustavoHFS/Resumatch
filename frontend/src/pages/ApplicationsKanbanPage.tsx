import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import { IconClose, IconExternal, IconFile, IconTrash } from '../components/Icons';
import type { ApplicationCard, KanbanStage } from '../types/api';
import styles from './ApplicationsKanbanPage.module.css';

type ColumnBucket = 'prep' | 'ready' | 'sent' | 'archived';

const COLUMNS: Array<{
  key: ColumnBucket;
  title: string;
  stages: KanbanStage[];
  targetStage: KanbanStage;
}> = [
  {
    key: 'prep',
    title: 'A preparar',
    stages: ['descoberta', 'candidatura_a_preparar'],
    targetStage: 'candidatura_a_preparar',
  },
  {
    key: 'ready',
    title: 'Currículo pronto',
    stages: ['curriculo_preparado', 'aguardando_envio_manual'],
    targetStage: 'curriculo_preparado',
  },
  {
    key: 'sent',
    title: 'Enviada',
    stages: ['enviada_manualmente'],
    targetStage: 'enviada_manualmente',
  },
  {
    key: 'archived',
    title: 'Arquivada',
    stages: ['encerrada_ou_descartada'],
    targetStage: 'encerrada_ou_descartada',
  },
];

const bucketForStage = (stage: KanbanStage): ColumnBucket => {
  for (const col of COLUMNS) {
    if (col.stages.includes(stage)) return col.key;
  }
  return 'prep';
};

export const ApplicationsKanbanPage: React.FC = () => {
  const [cards, setCards] = useState<ApplicationCard[]>([]);
  const [notice, setNotice] = useState<string | null>(null);

  const loadCards = useCallback(async () => {
    try {
      const res = await api.listApplications();
      setCards(res.items);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Erro ao carregar candidaturas.');
    }
  }, []);

  useEffect(() => {
    void loadCards();
  }, [loadCards]);

  const handleMoveStage = async (opportunityId: string, nextBucket: ColumnBucket) => {
    const col = COLUMNS.find((c) => c.key === nextBucket);
    if (!col) return;
    try {
      await api.transitionStage(opportunityId, {
        stage: col.targetStage,
        confirm_manual_submission: col.targetStage === 'enviada_manualmente',
      });
      await loadCards();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Não foi possível alterar o status.');
    }
  };

  const handleDelete = async (opportunityId: string) => {
    try {
      await api.deleteApplication(opportunityId);
      setCards((prev) => prev.filter((c) => c.opportunity_id !== opportunityId));
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Erro ao excluir candidatura.');
    }
  };

  return (
    <div className={styles.page}>
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

      <div className={styles.board}>
        {COLUMNS.map((col) => {
          const colCards = cards.filter((c) => col.stages.includes(c.stage));
          return (
            <section key={col.key} className={styles.column}>
              <div className={styles.colHead}>
                <h2 className={styles.colTitle}>{col.title}</h2>
                <span className={styles.colCount}>{colCards.length}</span>
              </div>

              <div className={styles.cardsList}>
                {colCards.length === 0 ? (
                  <div className={styles.emptyCol}>Nenhuma candidatura</div>
                ) : (
                  colCards.map((card) => (
                    <article key={card.opportunity_id} className={styles.card}>
                      <div className={styles.cardTop}>
                        <div>
                          <div className={styles.cardTitle}>{card.job.title}</div>
                          <div className={styles.cardCompany}>
                            {card.job.company || 'Empresa não informada'}
                            {card.job.location ? ` · ${card.job.location}` : ''}
                          </div>
                        </div>

                        <button
                          type="button"
                          className="iconBtn"
                          title="Remover candidatura"
                          onClick={() => void handleDelete(card.opportunity_id)}
                        >
                          <IconTrash size={14} />
                        </button>
                      </div>

                      <div className={styles.cardFooter}>
                        {col.key === 'ready' && (
                          <div className={styles.quickActions}>
                            {card.job.original_url && !card.job.original_url.startsWith('https://manual.resumatch.local/') && !card.job.original_url.includes('vaga-manual.local') && (
                              <a href={card.job.original_url} target="_blank" rel="noreferrer" className="btnSubtle btnSmall">Abrir Vaga ↗</a>
                            )}
                            <button type="button" className="btnGhost btnSmall" onClick={() => void handleMoveStage(card.opportunity_id, 'sent')}>
                              Já enviei ✓
                            </button>
                          </div>
                        )}
                        <select
                          className={`select ${styles.stageSelect}`}
                          value={bucketForStage(card.stage)}
                          aria-label="Mover status"
                          onChange={(e) =>
                            void handleMoveStage(
                              card.opportunity_id,
                              e.target.value as ColumnBucket
                            )
                          }
                        >
                          {COLUMNS.map((opt) => (
                            <option key={opt.key} value={opt.key}>
                              {opt.title}
                            </option>
                          ))}
                        </select>

                        <div className={styles.linksGroup}>
                          {card.resume_version?.export_rel_path && (
                            <a
                              href={`/api/resumes/versions/${encodeURIComponent(
                                card.resume_version.id
                              )}/pdf`}
                              target="_blank"
                              rel="noreferrer"
                              className="iconBtn"
                              title="Abrir PDF vinculado"
                            >
                              <IconFile size={15} />
                            </a>
                          )}

                          {card.job.original_url && (
                            <a
                              href={card.job.original_url}
                              target="_blank"
                              rel="noreferrer"
                              className="iconBtn"
                              title="Abrir anúncio original"
                            >
                              <IconExternal size={15} />
                            </a>
                          )}
                        </div>
                      </div>
                    </article>
                  ))
                )}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
};
