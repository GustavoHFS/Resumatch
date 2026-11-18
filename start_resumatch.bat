@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "scripts\start_app.py" %*
) else (
    python "scripts\start_app.py" %*
)

if errorlevel 1 (
    echo.
    echo [Resumatch] Process exited with an error. Press any key to close.
    pause >nul
)
