@echo off
REM ==================================================================
REM  Watchdog launcher - New Hire AI QA Assistant
REM  Content kept ASCII-only on purpose: the project path is not ASCII,
REM  so keeping this file pure ASCII avoids any codepage mangling.
REM  Real logic lives in watchdog.ps1 (UTF-8 with BOM).
REM ==================================================================
chcp 65001 >nul
cd /d "%~dp0"
title Watchdog - New Hire AI QA Assistant

where powershell >nul 2>nul
if errorlevel 1 (
    echo [ERROR] PowerShell was not found in PATH.
    pause
    exit /b 1
)

if not exist "%~dp0watchdog.ps1" (
    echo [ERROR] watchdog.ps1 was not found next to this script.
    pause
    exit /b 1
)

echo ========================================
echo   Watchdog - keeping 3 services alive
echo ========================================
echo.
echo   Qdrant   6333   Backend 8000   Frontend 5173
echo   Check interval: 15s (auto restart on failure)
echo.
echo   Close this window to stop guarding.
echo   To stop everything use the stop script.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog.ps1" %*

echo.
echo [Watchdog] exited.
pause
