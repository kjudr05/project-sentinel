@echo off
title Project Sentinel Setup
color 0A
echo.
echo  =====================================================
echo   Project Sentinel ^| Samsung PRISM AX Hackathon
echo   Windows One-Click Setup
echo  =====================================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found. Install from python.org/downloads
    echo  Tick "Add Python to PATH" during install.
    pause & exit /b 1
)
for /f "tokens=*" %%v in ('python --version') do echo  [OK] %%v

node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Node.js not found. Install from nodejs.org
    pause & exit /b 1
)
for /f "tokens=*" %%v in ('node --version') do echo  [OK] Node.js %%v

echo.
echo  Installing Python packages...
pip install anthropic psutil pytest --quiet
echo  [OK] Packages installed.

echo.
echo  Installing OpenClaw...
call npm install -g openclaw@latest --silent 2>nul
for /f "tokens=*" %%v in ('openclaw --version 2^>nul') do echo  [OK] %%v

if not exist "logs" mkdir logs
echo  [OK] logs\ directory ready.

echo.
echo  Running 133 tests...
python -m pytest tests/ -q --tb=short
if %errorlevel% equ 0 (echo  [OK] All tests passed.) else (echo  [WARN] Some tests failed.)

echo.
echo  =====================================================
echo   Done! Quick start:
echo.
echo   python scripts\demo.py --quick
echo   dashboard\sentinel_web.html  (double-click)
echo   python agent\sentinel_agent.py --mock warn
echo  =====================================================
echo.
pause
