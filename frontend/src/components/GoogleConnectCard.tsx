import React, { useState } from 'react';
import { api } from '../api/client';
import { IconCheck, IconExternal } from './Icons';
import styles from './GoogleConnectCard.module.css';

interface Props {
  connected: boolean;
  onStatusChanged: () => void;
  compact?: boolean;
}

export const GoogleConnectCard: React.FC<Props> = ({
  connected,
  onStatusChanged,
  compact = false,
}) => {
  const [keyInput, setKeyInput] = useState('');
  const [validating, setValidating] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [editingWhenConnected, setEditingWhenConnected] = useState(false);

  const connectWithKey = async (rawValue: string) => {
    const trimmed = rawValue.trim();
    if (trimmed.length < 15) {
      setFeedback('Cole a chave completa gerada na página do Google.');
      return;
    }
    setValidating(true);
    setFeedback(null);
    try {
      await api.connectGoogleAi(trimmed);
      setKeyInput('');
      setEditingWhenConnected(false);
      setFeedback('Conectado ao Google. Tudo pronto para adaptar seus currículos.');
      onStatusChanged();
    } catch (err) {
      setFeedback(
        err instanceof Error
          ? err.message
          : 'Não foi possível validar sua chave. Confira se copiou o código completo.'
      );
    } finally {
      setValidating(false);
    }
  };

  const handlePasteFromClipboard = async () => {
    try {
      const clipText = await navigator.clipboard.readText();
      if (!clipText.trim()) {
        setFeedback('Sua área de transferência está vazia. Copie o código no Google primeiro.');
        return;
      }
      setKeyInput(clipText.trim());
      await connectWithKey(clipText);
    } catch {
      setFeedback('Use Ctrl+V no campo abaixo para colar seu código.');
    }
  };

  const handleDisconnect = async () => {
    setValidating(true);
    setFeedback(null);
    try {
      await api.disconnectGoogleAi();
      setEditingWhenConnected(false);
      onStatusChanged();
    } catch (err) {
      setFeedback(err instanceof Error ? err.message : 'Erro ao desconectar.');
    } finally {
      setValidating(false);
    }
  };

  if (connected && !editingWhenConnected) {
    return (
      <div className={styles.connectedBar}>
        <div className={styles.connectedInfo}>
          <IconCheck size={15} />
          <span>Conectado ao Google</span>
        </div>
        <div className={styles.connectedActions}>
          <button
            type="button"
            className="btnSubtle btnSmall"
            onClick={() => setEditingWhenConnected(true)}
          >
            Trocar
          </button>
          <button
            type="button"
            className="btnSubtle btnSmall"
            disabled={validating}
            onClick={() => void handleDisconnect()}
          >
            Desconectar
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className={`${styles.card} ${compact ? styles.cardCompact : ''}`}>
      <div className={styles.header}>
        <h3 className={styles.title}>Conectar com Google Gratuitamente</h3>
        {connected && (
          <button
            type="button"
            className="btnSubtle btnSmall"
            onClick={() => {
              setEditingWhenConnected(false);
              setFeedback(null);
            }}
          >
            Cancelar
          </button>
        )}
      </div>

      <p className={styles.subtitle}>
        Ative a geração automática de currículos com sua conta gratuita do Google em 2 cliques:
      </p>

      <div className={styles.stepRow}>
        <span className={styles.stepLabel}>Passo 1</span>
        <a
          href="https://aistudio.google.com/apikey"
          target="_blank"
          rel="noreferrer"
          className="btnGhost btnSmall"
        >
          <span>Obter acesso gratuito no Google</span>
          <IconExternal size={13} />
        </a>
      </div>

      <div className={styles.stepColumn}>
        <span className={styles.stepLabel}>Passo 2</span>
        <div className={styles.pasteRow}>
          <input
            type="password"
            className="input"
            placeholder="Cole aqui sua chave gratuita do Google"
            value={keyInput}
            disabled={validating}
            onChange={(e) => setKeyInput(e.target.value)}
            onPaste={(e) => {
              const pasted = e.clipboardData.getData('text').trim();
              if (pasted.length >= 20) {
                e.preventDefault();
                setKeyInput(pasted);
                void connectWithKey(pasted);
              }
            }}
          />
          <button
            type="button"
            className="btnSubtle"
            disabled={validating}
            onClick={() => void handlePasteFromClipboard()}
          >
            {validating ? 'Validando...' : 'Colar da Área de Transferência'}
          </button>
          {keyInput.trim().length >= 15 && !validating && (
            <button
              type="button"
              className="btnGhost"
              onClick={() => void connectWithKey(keyInput)}
            >
              Ativar
            </button>
          )}
        </div>
      </div>

      {feedback && <p className={styles.feedback}>{feedback}</p>}
    </div>
  );
};
