# Resumatch

Aplicativo local para descobrir vagas de TI, adaptar currículos A4 com revisão humana e acompanhar candidaturas. Os dados pessoais, o banco SQLite, as chaves e os PDFs permanecem em `user_data/` — nunca são enviados ao Git.

## Instalação em um passo (Windows)

1. Instale Python 3.12+, Node.js 20+ (com npm) e o .NET Framework 4.x com compilador `csc.exe`.
2. Dê duplo clique em **`Instalar.bat`** na raiz do projeto.
3. Abra **`Resumatch.exe`**. O Resumatch inicia o servidor local e abre o navegador padrão em `http://127.0.0.1:8000/` (ou na próxima porta disponível).

O instalador cria `.venv`, instala `backend/requirements.txt` (que referencia o `pyproject.toml`), instala o Chromium headless do Playwright, executa `npm install` e `npm run build` em `frontend/`, e compila o launcher nativo. É seguro repetir o comando: não apaga nem substitui arquivos em `user_data/`. É necessário acesso à internet na primeira instalação.

## Primeiro arranque e dados

Se `user_data/` não existir, o backend cria `config/`, `profile/`, `db/` e `exports/` a partir dos exemplos **vazios** em `examples/`. Complete o Currículo Base e configure suas cidades e empresas bloqueadas no aplicativo. Se algum arquivo local já existir, o bootstrap não o sobrescreve. Nunca use os exemplos de testes (`backend/tests/fixtures/`) como dados pessoais.

## Estrutura

```text
backend/app/       API, domínio, conectores, serviços e templates
backend/tests/     testes e fixtures sintéticas isoladas
backend/requirements.txt  instalação Python (dependências no pyproject.toml)
frontend/src/      React, TypeScript e CSS local
examples/          exemplos iniciais sem perfil ou bloqueios
scripts/           launcher, privacidade e migrações opcionais
Instalar.bat       instalação Windows em um passo
user_data/         dados locais ignorados pelo Git
Resumatch.exe      launcher gerado e ignorado pelo Git
```

## Desenvolvimento e verificação

```powershell
.venv\Scripts\python.exe scripts\start_app.py
npm --prefix frontend run build
.venv\Scripts\pytest -v
.venv\Scripts\python.exe scripts\check_privacy.py
```

A busca usa fontes públicas; disponibilidade e resultados variam por data e localização. A integração com Google Gemini é opcional e mascara contatos antes de enviar texto à IA. O envio de candidaturas permanece manual.
