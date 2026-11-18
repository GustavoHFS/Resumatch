# Resumatch (Português — Brasil)

> **Idioma / Language:** [English](./README.md) | **Português (Brasil)**

O **Resumatch** é uma aplicação completa *local-first* para descoberta assíncrona de vagas, triagem determinística em 3 eixos, adaptação híbrida de currículos A4 legíveis por ATS e gestão auditada de candidaturas em Kanban — construída com **FastAPI**, **SQLModel (SQLite em modo WAL)**, **Pydantic v2**, **Playwright Headless Chromium** e **React + TypeScript + Vanilla CSS Puro**.

Projetado para substituir fluxos manuais baseados em arquivos `.docx` e chamadas repetitivas de IA, o Resumatch executa **mais de 95% da inteligência de triagem e montagem com Zero IA**, oferecendo opcionalmente um **Modo IA Cirúrgica** em chamada única protegido por mascaramento reversível de PII e validação pós-LLM de veracidade factual.

---

## Pilares de Engenharia e Arquitetura

### 1. Motor Determinístico de Busca e Triagem em 3 Eixos (Zero IA)
Conectores assíncronos (`httpx.AsyncClient` + `truststore` + `selectolax`) coletam fontes públicas `https://` respeitando `robots.txt`, *rate-limiting* por domínio, orçamento máximo de requisições e aborto limpo em HTTP `403/429`/CAPTCHA. Cada oportunidade descoberta ou importada manualmente é avaliada localmente em milissegundos nos 3 eixos independentes:
- **Eixo 1 — Compatibilidade Profissional:** Extração lexical sensível a seções (requisitos obrigatórios vs. desejáveis e negações), senioridade, política geográfica por cidades/bairros, janela de horário diurno, regime de contrato e lista privada de empresas bloqueadas. Classifica competências em `confirmed_practice`, `academic_foundation`, `preparable_gap` (exibindo o `study_plan` interno na UI para preparação pré-entrevista) e `unconfirmed`.
- **Eixo 2 — Situação Operacional e Omissões:** Separa impedimentos concretos de omissões operacionais do anúncio (ex.: salário ou detalhes de presencialidade omitidos), sem bloquear o preparo local do currículo.
- **Eixo 3 — Níveis Éticos de Automação (1 / 2 / 3):** Classifica cada vaga entre Nível 1 (descoberta + canal direto), Nível 2 (preparação local assistida) e Nível 3 (portais com login), proibindo submissões automáticas cegas ou contorno de mecanismos anti-bot.

### 2. Estúdio Híbrido de Currículo (`Modo Determinístico` + `Modo IA Cirúrgica`)
- **Modo Determinístico (`< 20ms`, Zero Tokens):** Pontua e seleciona *bullets*, grupos de competências, cursos concluídos e resumo em `pt-BR` ou `en-US` (incluindo `suporte-en`) a partir do `MasterProfile`, aplicando equivalências contextuais auditadas e reordenação dinâmica de seções.
- **Modo IA Cirúrgica (1 Chamada Única + Zero PII):**
  - **`PIIStripper`:** Remove integralmente `header` (nome, e-mail, telefone, localização e links) e `blocked_companies`, mascarando empregadores e instituições acadêmicas com *placeholders* reversíveis (`[[EMP_*]]`, `[[INST_*]]`) antes de acionar `GeminiAdapter`, `GroqAdapter` ou `OllamaAdapter` local via `httpx` puro (zero SDKs pesados).
  - **`VeracityGuard`:** Reidrata os *placeholders* localmente e inspeciona cada bloco de texto individualmente. Qualquer tentativa de injetar skills `preparable_gap`/`unconfirmed`, projetos `reserved`, termos autodepreciativos ou **números/métricas inventadas** sofre *fallback* imediato para o texto determinístico original, exibindo o `diff` completo (`before` vs `after`) na interface.

### 3. Exportação PDF A4 Offline e Validação Automática de ATS
- Template HTML5 semântico com **Vanilla CSS Puro** (`@page { size: A4 portrait }`, coluna única linear sem armadilhas de leitura para robôs ATS) e fontes `Libre Franklin` `.woff2` embutidas offline.
- Pré-visualização A4 ao vivo isolada na interface com **medidor instantâneo de altura/estouro de página (`max_pages`)**, seguida de renderização via Chromium Headless sob demanda (`auto_fit` em até 4 estágios) e auditoria automática via `pypdf` (contagem exata de páginas, extração linear de texto, links clicáveis `/Subtype /Link` e hash **SHA-256** imutável vinculado ao Kanban).

### 4. Isolamento de Dados e Privacidade por Design
- **Isolamento Estrito em `./user_data/`:** Perfis reais, banco SQLite, backups rotativos atômicos e PDFs exportados vivem exclusivamente em `./user_data/` (ignorado no `.gitignore` e no `.dockerignore`).
- **Persona Fictícia de Demonstração:** A pasta `examples/` traz dados 100% fictícios (*Marina Alves Costa*, Belo Horizonte/MG) prontos para demonstração pública.
- **Pre-Commit Hook Ativo:** `scripts/check_privacy.py` bloqueia automaticamente commits contendo padrões de PII, arquivos sensíveis ou *fingerprints* dinâmicos locais.

---

## Como Executar — Opção 1: Docker (1 Comando)

```bash
docker compose up --build
```

Acesse `http://localhost:8000`. Caso `./user_data/` esteja vazio, o container executa `scripts/bootstrap_demo.py` automaticamente com os dados fictícios de `examples/` (*Marina Alves Costa*).

---

## Como Executar — Opção 2: Modo Aplicativo Local em 1 Clique

### Pré-requisitos
- Python `3.12+`
- Node.js `20+` / `22 LTS`

### Instalação Inicial
```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
playwright install chromium-headless-shell

# Ativar o pre-commit hook de privacidade e popular dados demo (se user_data/ estiver vazio):
python scripts/install_hooks.py
python scripts/bootstrap_demo.py
```

### Inicialização em 1 Clique
- **Windows:** Dê duplo clique em `start_resumatch.bat` na raiz do projeto (ou execute `python scripts/start_app.py`).
- **Terminal Multiplataforma:**
  ```bash
  python scripts/start_app.py
  ```
O `start_app.py` valida o build estático em `frontend/dist`, sobe o backend FastAPI servindo a API e a SPA em `http://127.0.0.1:8000` e abre automaticamente uma janela dedicada de aplicativo (`Edge/Chrome --app`). A documentação interativa da API permanece acessível em `http://127.0.0.1:8000/docs`.

---

## Verificação de Qualidade e Privacidade

```bash
# 1. Checagem estrita de tipos TypeScript e build de produção da SPA
cd frontend && npm run build && cd ..

# 2. Suíte completa de testes automatizados do backend e motor PDF/ATS
pytest -v

# 3. Auditoria estática anti-vazamento de dados pessoais
python scripts/check_privacy.py
```
