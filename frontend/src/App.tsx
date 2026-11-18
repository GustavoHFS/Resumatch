import React, { useCallback, useEffect, useState } from 'react';
import { api } from './api/client';
import styles from './App.module.css';
import { ApplicationsKanbanPage } from './pages/ApplicationsKanbanPage';
import { DashboardJobsPage } from './pages/DashboardJobsPage';
import { ProfileSettingsPage } from './pages/ProfileSettingsPage';
import { ResumeStudioPage } from './pages/ResumeStudioPage';
import type { HealthResponse, JobListItem, MasterProfile } from './types/api';

type AppRoute = 'dashboard' | 'studio' | 'kanban' | 'profile';

const ROUTE_TITLES: Record<AppRoute, { title: string; sub: string }> = {
  dashboard: {
    title: 'Painel & Caixa de Entrada de Vagas',
    sub: 'Busca assíncrona HTTPS, triagem determinística em 3 eixos e planos internos de estudo',
  },
  studio: {
    title: 'Estúdio Híbrido de Currículo A4',
    sub: 'Composição determinística (< 20ms), IA Cirúrgica c/ PIIStripper + VeracityGuard e folha A4 ao vivo',
  },
  kanban: {
    title: 'Kanban Auditado de Candidaturas',
    sub: 'Ciclo de vida com hash SHA-256 imutável do PDF e confirmação explícita de envio manual',
  },
  profile: {
    title: 'Perfil Mestre & Configurações Locais',
    sub: 'Editor visual atômico substituindo currículos .docx manuais, com backups rotativos em user_data/',
  },
};

export const App: React.FC = () => {
  const [route, setRoute] = useState<AppRoute>('dashboard');
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [profile, setProfile] = useState<MasterProfile | null>(null);

  const [studioTargetJob, setStudioTargetJob] = useState<JobListItem | null>(null);
  const [studioTargetArea, setStudioTargetArea] = useState<string | null>(null);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

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

  const handleOpenInStudio = (job: JobListItem, suggestedAreaId?: string | null) => {
    setStudioTargetJob(job);
    setStudioTargetArea(suggestedAreaId ?? job.area_id ?? null);
    setRoute('studio');
  };

  const meta = ROUTE_TITLES[route];

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-5)' }}>
          <div className={styles.brandBlock}>
            <span className={styles.brandName}>Resumatch</span>
            <span className={styles.brandTagline}>
              Local-First Job Triage &amp; ATS A4 Studio
            </span>
          </div>

          <nav className={styles.navMenu} aria-label="Navegação Principal">
            <button
              type="button"
              className={`${styles.navItem} ${route === 'dashboard' ? styles.navItemActive : ''}`}
              onClick={() => setRoute('dashboard')}
            >
              <span>Caixa de Vagas</span>
              {health && (
                <span className="badge badgeNeutral">{health.counts.opportunities}</span>
              )}
            </button>

            <button
              type="button"
              className={`${styles.navItem} ${route === 'studio' ? styles.navItemActive : ''}`}
              onClick={() => setRoute('studio')}
            >
              <span>Estúdio A4</span>
              {profile && (
                <span className="badge badgeInfo">
                  {Object.keys(profile.area_presets).length} presets
                </span>
              )}
            </button>

            <button
              type="button"
              className={`${styles.navItem} ${route === 'kanban' ? styles.navItemActive : ''}`}
              onClick={() => setRoute('kanban')}
            >
              <span>Kanban</span>
              {health && (
                <span className="badge badgeNeutral">{health.counts.tracked_applications}</span>
              )}
            </button>

            <button
              type="button"
              className={`${styles.navItem} ${route === 'profile' ? styles.navItemActive : ''}`}
              onClick={() => setRoute('profile')}
            >
              <span>Perfil &amp; Configs</span>
            </button>
          </nav>
        </div>

        <div className={styles.sidebarFooter}>
          <button
            type="button"
            className="btn btnSmall"
            onClick={() => setTheme((prev) => (prev === 'dark' ? 'light' : 'dark'))}
          >
            Tema: {theme === 'dark' ? 'Escuro Sóbrio' : 'Claro Editorial'}
          </button>

          {health?.bootstrapped_demo && (
            <span className="badge badgeWarn">Modo Demo (examples/)</span>
          )}

          <div>
            SQLite WAL • IA: <strong>{health?.llm_provider ?? 'none'}</strong>
          </div>
          <div>Dados isolados em <code>./user_data/</code></div>
        </div>
      </aside>

      <div className={styles.mainArea}>
        <header className={styles.topBar}>
          <div>
            <h1 className={styles.pageTitle}>{meta.title}</h1>
            <p className={styles.pageSub}>{meta.sub}</p>
          </div>
          {profile && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="badge badgeInfo">{profile.header.full_name}</span>
              <span className="badge badgeNeutral">
                {profile.header.city}/{profile.header.state}
              </span>
            </div>
          )}
        </header>

        <main className={styles.contentBody}>
          {route === 'dashboard' && (
            <DashboardJobsPage
              health={health}
              areaPresets={profile?.area_presets ?? {}}
              onRefreshHealth={refreshHealth}
              onOpenInStudio={handleOpenInStudio}
            />
          )}

          {route === 'studio' && (
            <ResumeStudioPage
              profile={profile}
              initialJob={studioTargetJob}
              initialAreaId={studioTargetArea}
              onRefreshHealth={refreshHealth}
            />
          )}

          {route === 'kanban' && <ApplicationsKanbanPage onRefreshHealth={refreshHealth} />}

          {route === 'profile' && (
            <ProfileSettingsPage
              profile={profile}
              onProfileSaved={(updated) => setProfile(updated)}
              onRefreshHealth={refreshHealth}
            />
          )}
        </main>
      </div>
    </div>
  );
};
