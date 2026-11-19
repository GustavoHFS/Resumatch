import React, { useCallback, useEffect, useState } from 'react';
import { api } from './api/client';
import styles from './App.module.css';
import {
  IconBoardCheck,
  IconBriefcase,
  IconGear,
  IconStudioDoc,
  IconUserDoc,
} from './components/Icons';
import { SettingsDrawer } from './components/SettingsDrawer';
import { ApplicationsKanbanPage } from './pages/ApplicationsKanbanPage';
import { BaseResumePage } from './pages/BaseResumePage';
import { JobsPage } from './pages/JobsPage';
import { ResumeStudioPage } from './pages/ResumeStudioPage';
import type { HealthResponse, JobListItem, MasterProfile } from './types/api';

type NavTab = 'jobs' | 'studio' | 'applications' | 'profile';

const NAV_ITEMS: Array<{ key: NavTab; label: string; Icon: React.FC<{ size?: number }> }> = [
  { key: 'jobs', label: 'Vagas', Icon: IconBriefcase },
  { key: 'studio', label: 'Estúdio', Icon: IconStudioDoc },
  { key: 'applications', label: 'Candidaturas', Icon: IconBoardCheck },
  { key: 'profile', label: 'Currículo Base', Icon: IconUserDoc },
];

export const App: React.FC = () => {
  const [activeTab, setActiveTab] = useState<NavTab>('jobs');
  const [settingsOpen, setSettingsOpen] = useState(false);

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [profile, setProfile] = useState<MasterProfile | null>(null);

  const [studioJob, setStudioJob] = useState<JobListItem | null>(null);
  const [studioJobDescription, setStudioJobDescription] = useState<string>('');

  const refreshHealth = useCallback(() => {
    api
      .getHealth()
      .then(setHealth)
      .catch(() => {});
  }, []);

  const refreshProfile = useCallback(() => {
    api
      .getProfile()
      .then(setProfile)
      .catch(() => {});
  }, []);

  useEffect(() => {
    refreshHealth();
    refreshProfile();
  }, [refreshHealth, refreshProfile]);

  useEffect(() => {
    const clientId =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `tab_${Math.random().toString(36).slice(2, 10)}`;

    void api.pingLifecycle(clientId).catch(() => {});
    const hbTimer = window.setInterval(() => {
      void api.pingLifecycle(clientId).catch(() => {});
    }, 4000);

    const handlePageHide = () => {
      try {
        const blob = new Blob([JSON.stringify({ client_id: clientId })], {
          type: 'application/json',
        });
        navigator.sendBeacon('/api/lifecycle/close', blob);
      } catch {
        // ignore unload errors
      }
    };

    window.addEventListener('pagehide', handlePageHide);
    return () => {
      window.clearInterval(hbTimer);
      window.removeEventListener('pagehide', handlePageHide);
    };
  }, []);

  const handleAdaptJob = (job: JobListItem, descriptionText: string) => {
    setStudioJob(job);
    setStudioJobDescription(descriptionText);
    setActiveTab('studio');
  };
  const handleProfileSaved = useCallback((updated: MasterProfile) => setProfile(updated), []);

  return (
    <div className={styles.appShell}>
      <header className={styles.topBar}>
        <button
          type="button"
          className={styles.wordmark}
          onClick={() => setActiveTab('jobs')}
        >
          <img
            src="/logo-chibi.svg"
            alt=""
            width={30}
            height={30}
            className={styles.brandLogo}
          />
          <span className={styles.wordmarkText}>Resumatch</span>
        </button>

        <nav className={styles.navLinks} aria-label="Navegação principal">
          {NAV_ITEMS.map(({ key, label, Icon }) => (
            <button
              key={key}
              type="button"
              className={`${styles.navLink} ${
                activeTab === key ? styles.navLinkActive : ''
              }`}
              onClick={() => setActiveTab(key)}
            >
              <Icon size={17} />
              <span>{label}</span>
            </button>
          ))}
        </nav>

        <div className={styles.rightSlot}>
          <button
            type="button"
            className="iconBtn"
            title="Configurações"
            aria-label="Configurações"
            onClick={() => setSettingsOpen(true)}
          >
            <IconGear size={18} />
          </button>
        </div>
      </header>

      <main className={styles.mainContent}>
        <div className={styles.pageStage}>
          {activeTab === 'jobs' && <JobsPage onAdaptJob={handleAdaptJob} />}

          <div hidden={activeTab !== 'studio'}>
            <ResumeStudioPage
              aiConnected={Boolean(health?.ai_connected)}
              initialJob={studioJob}
              initialJobDescription={studioJobDescription}
              onAiConnected={refreshHealth}
            />
          </div>

          {activeTab === 'applications' && <ApplicationsKanbanPage />}

          <div hidden={activeTab !== 'profile'}>
            <BaseResumePage
              profile={profile}
              onProfileSaved={handleProfileSaved}
            />
          </div>
        </div>
      </main>

      <SettingsDrawer
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onLlmUpdated={refreshHealth}
      />
    </div>
  );
};
