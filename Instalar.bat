@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo Bem-vindo ao Resumatch! Vamos preparar o aplicativo nesta pasta.
echo Seus dados pessoais em user_data serao preservados.
echo E necessario Python 3.12 ou superior e internet na primeira instalacao.

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

if exist "frontend\dist\index.html" (
    echo Interface pronta encontrada. Node.js nao sera necessario.
    goto :launcher
)
where npm >nul 2>nul
if errorlevel 1 (
    echo Para compilar a interface, instale Node.js 22+ e execute Instalar.bat novamente.
    goto :failed
)
pushd frontend
call npm ci --no-audit --no-fund
if errorlevel 1 (popd & goto :failed)
call npm run build
if errorlevel 1 (popd & goto :failed)
popd

:launcher
if exist "Resumatch.exe" goto :done
".venv\Scripts\python.exe" scripts\build_launcher_exe.py
if errorlevel 1 goto :failed
:done
echo Instalacao concluida. Abra Resumatch.exe para iniciar.
pause
exit /b 0

:failed
echo A instalacao falhou. Nenhum dado em user_data foi alterado por este instalador.
echo Confira a mensagem acima. Instale Python 3.12+ se ele nao estiver disponivel.
pause
exit /b 1
