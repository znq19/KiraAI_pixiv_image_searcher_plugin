@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo Please install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
)

echo Installing dependencies...
.venv\Scripts\pip install -r requirements.txt

echo Starting Pixiv token tool...
.venv\Scripts\python get_pixiv_token.py
if errorlevel 1 (
    echo.
    echo If the error says no browser found, please run:
    echo   .venv\Scripts\python -m playwright install chromium
    echo Then run this batch file again.
)

pause
