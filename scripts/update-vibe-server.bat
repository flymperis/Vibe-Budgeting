@echo off
setlocal

REM Run from this script's folder, then execute PowerShell updater
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\update-vibe-server.ps1"

echo.
echo Update process finished. Press any key to close.
pause >nul

