@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if errorlevel 1 goto :failed
)

set "PATH=%CD%\.venv\Scripts;%PATH%"
".venv\Scripts\python.exe" -m pip install -r backend\requirements.txt
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m playwright install chromium-headless-shell
if errorlevel 1 goto :failed

where npm >nul 2>nul
if errorlevel 1 (
    echo Node.js e npm sao necessarios. Instale Node.js 20+ e execute setup.bat novamente.
    goto :failed
)
pushd frontend
call npm install --no-audit --no-fund
if errorlevel 1 (popd & goto :failed)
call npm run build
if errorlevel 1 (popd & goto :failed)
popd

".venv\Scripts\python.exe" scripts\build_launcher_exe.py
if errorlevel 1 goto :failed
echo Instalacao concluida. Abra Resumatch.exe para iniciar.
exit /b 0

:failed
echo A instalacao falhou. Nenhum dado em user_data foi alterado por este instalador.
exit /b 1
