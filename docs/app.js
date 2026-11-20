'use strict';

// Deliberately synthetic. No API, storage, telemetry, or external requests.
const jobs = [
  { id: 'support', title: 'Suporte Técnico de TI Júnior', company: 'Horizonte Digital', level: 'Júnior', source: 'Gupy', location: 'Remoto · Hoje', tags: ['Windows', 'Suporte', 'SQL'], description: 'Atendimento de chamados, orientação de usuários e documentação de soluções. Conhecimentos básicos de Windows e SQL. Oportunidade de início de carreira com acompanhamento da equipe.' },
  { id: 'dev', title: 'Estágio em Desenvolvimento Python', company: 'Órbita Tecnologia', level: 'Estágio', source: 'GitHub', location: 'Híbrido · Há 1 dia', tags: ['Python', 'Git', 'APIs REST'], description: 'Apoio na construção de APIs REST, testes e documentação. Buscamos estudantes com projetos acadêmicos em Python e Git. Não exige experiência profissional anterior.' },
  { id: 'data', title: 'Analista de Dados Júnior', company: 'Ponto de Dados', level: 'Júnior', source: 'LinkedIn', location: 'Remoto · Há 2 dias', tags: ['SQL', 'Python', 'Dados'], description: 'Organização de bases, consultas SQL e apoio a análises com Python. Valorizamos projetos de estudo, atenção à qualidade dos dados e disposição para aprender.' },
  { id: 'infra', title: 'Estágio em Infraestrutura', company: 'Nuvem Escola', level: 'Estágio', source: 'Gupy', location: 'Presencial · Há 3 dias', tags: ['Windows', 'Suporte', 'Documentação'], description: 'Apoio ao suporte de estações e organização de chamados. Estágio supervisionado para estudantes de Tecnologia. Atividades de documentação e atendimento interno.' },
];
const stages = ['A preparar', 'Currículo pronto', 'Enviada', 'Arquivada'];
const saved = new Map();
let selectedJob = jobs[0].id;
let studioJob = '';
let adaptedJob = null;
const $ = (id) => document.getElementById(id);
const status = (message) => { $('status').textContent = message; };

function showTab(tab, focus = false) {
  document.querySelectorAll('[data-tab]').forEach((button) => {
    const active = button.dataset.tab === tab;
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
    $(`panel-${button.dataset.tab}`).hidden = !active;
    if (active && focus) button.focus();
  });
  if (tab === 'applications') renderBoard();
  if (tab === 'studio') renderStudioOptions();
}

function saveJob(id) {
  if (!saved.has(id)) saved.set(id, { stage: stages[0], pdf: false });
  $('saved-count').textContent = String(saved.size);
  renderJobs();
  renderStudioOptions();
  status('02 · Vaga salva em Candidaturas. Abra o cartão no Estúdio para continuar.');
}

function renderJobs() {
  const query = $('search').value.trim().toLocaleLowerCase('pt-BR');
  const filtered = jobs.filter((job) => (!query || `${job.title} ${job.company} ${job.tags.join(' ')}`.toLocaleLowerCase('pt-BR').includes(query)) && (!$('level').value || job.level === $('level').value) && (!$('source').value || job.source === $('source').value));
  if (!filtered.some((job) => job.id === selectedJob)) selectedJob = filtered[0]?.id || '';
  $('job-list').replaceChildren();
  if (!filtered.length) {
    $('job-list').textContent = 'Nenhuma vaga demonstrativa para estes filtros.';
    $('job-detail').textContent = 'Experimente limpar os filtros.';
    return;
  }
  filtered.forEach((job) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'job-row';
    button.setAttribute('aria-pressed', String(job.id === selectedJob));
    // Only static, developer-owned demo data enters this template.
    button.innerHTML = `<b>${job.title}</b><small>${job.company} · ${job.source} · ${job.location}</small>`;
    button.addEventListener('click', () => { selectedJob = job.id; renderJobs(); });
    $('job-list').append(button);
  });
  const job = jobs.find((item) => item.id === selectedJob);
  $('job-detail').innerHTML = `<div class="eyebrow">${job.source.toUpperCase()} · EXEMPLO FICTÍCIO</div><h3>${job.title}</h3><p>${job.company} · ${job.location}</p><div class="tags">${job.tags.map((tag) => `<span class="tag">${tag}</span>`).join('')}</div><p>${job.description}</p><div class="actions"><button class="button primary" id="save-job">${saved.has(job.id) ? 'Salva em Candidaturas ✓' : 'Salvar em Candidaturas'}</button><button class="button" id="open-job">Adaptar no Estúdio ✨</button></div>`;
  $('save-job').addEventListener('click', () => saveJob(job.id));
  $('open-job').addEventListener('click', () => { saveJob(job.id); openStudio(job.id); });
}

function renderBoard() {
  $('board').replaceChildren();
  stages.forEach((stage) => {
    const column = document.createElement('section');
    column.className = 'column';
    const entries = [...saved].filter(([, card]) => card.stage === stage);
    column.innerHTML = `<h3>${stage}<span>${entries.length}</span></h3>`;
    if (!entries.length) {
      const empty = document.createElement('p');
      empty.className = 'fine';
      empty.textContent = 'Nenhuma candidatura';
      column.append(empty);
    }
    entries.forEach(([id, card]) => {
      const job = jobs.find((item) => item.id === id);
      const article = document.createElement('article');
      article.className = 'application-card';
      article.innerHTML = `<h4>${job.title}</h4><p>${job.company}${card.pdf ? ' · PDF demonstrativo vinculado ✓' : ''}</p><button class="button">Abrir no Estúdio ✨</button>`;
      article.querySelector('button').addEventListener('click', () => openStudio(id));
      column.append(article);
    });
    $('board').append(column);
  });
}

function renderStudioOptions() {
  const select = $('studio-job');
  select.replaceChildren(new Option('Colar descrição avulsa abaixo', ''));
  saved.forEach((card, id) => {
    const job = jobs.find((item) => item.id === id);
    select.add(new Option(`${job.title} — ${job.company} (${card.stage})`, id));
  });
  select.value = studioJob;
}

function openStudio(id) {
  studioJob = id;
  adaptedJob = null;
  $('description').value = jobs.find((job) => job.id === id).description;
  $('target-role').textContent = jobs.find((job) => job.id === id).title;
  showTab('studio', true);
  status('03 · Descrição carregada. Teste a fonte, o mascaramento e a adaptação demonstrativa.');
}

document.querySelectorAll('[data-tab]').forEach((button, index, tabs) => {
  button.addEventListener('click', () => showTab(button.dataset.tab));
  button.addEventListener('keydown', (event) => {
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    if (next !== undefined) { event.preventDefault(); showTab(tabs[next].dataset.tab, true); }
  });
});
['search', 'level', 'source'].forEach((id) => $(id).addEventListener('input', renderJobs));
$('clear-filters').addEventListener('click', () => {
  ['search', 'level', 'source'].forEach((id) => { $(id).value = ''; });
  renderJobs();
});
$('studio-job').addEventListener('change', (event) => {
  studioJob = event.target.value;
  adaptedJob = null;
  const job = jobs.find((item) => item.id === studioJob);
  $('description').value = job?.description || '';
  $('target-role').textContent = job?.title || 'Estágio em Tecnologia';
});
$('description').addEventListener('input', () => { adaptedJob = null; });
$('font').addEventListener('change', (event) => {
  $('paper').style.fontFamily = `"${event.target.value}", ${event.target.value === 'Times New Roman' ? 'serif' : 'sans-serif'}`;
});
$('mask').addEventListener('click', () => {
  const masked = $('mask').getAttribute('aria-pressed') !== 'true';
  $('mask').setAttribute('aria-pressed', String(masked));
  $('mask').textContent = masked ? 'Ver dados fictícios originais' : 'Ver texto mascarado';
  $('privacy-preview').textContent = masked ? '[NOME_CANDIDATO]\n[EMAIL]\n[TELEFONE]' : 'Candidata Exemplo\ncandidata@example.com\n(00) 90000-0000';
});
$('adapt').addEventListener('click', () => {
  const job = jobs.find((item) => item.id === studioJob);
  if (!$('description').value.trim()) { status('Cole uma descrição fictícia antes de adaptar.'); return; }
  const focus = job?.tags.join(', ') || 'Python, SQL e suporte técnico';
  $('resume-summary').textContent = `Estudante de Sistemas de Informação com projetos acadêmicos em ${focus}. Prática de documentação e organização de chamados em projeto de estudos. Busca oportunidade de início de carreira para aplicar esses conhecimentos com supervisão.`;
  adaptedJob = studioJob;
  status('04 · Rascunho demonstrativo adaptado, sem IA externa. Revise a folha e simule o PDF pronto.');
});
$('demo-pdf').addEventListener('click', () => {
  if (adaptedJob !== studioJob) { status('Adapte a demonstração antes de simular a exportação do PDF.'); return; }
  if (!studioJob) { status('Prévia avulsa concluída. Esta demo não gera um arquivo PDF real.'); return; }
  const card = saved.get(studioJob);
  card.stage = stages[1];
  card.pdf = true;
  renderStudioOptions();
  status('PDF simulado vinculado: candidatura movida para Currículo pronto. Nenhum arquivo real foi gerado.');
});
renderJobs();
renderBoard();
