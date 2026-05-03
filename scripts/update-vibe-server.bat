@echo off
setlocal
title Vibe Budgeting updater

echo Deploy root = current directory:
echo   %CD%
echo.
echo If this is wrong: open CMD, run:
echo   cd \path\to\folder\that\has\docker-compose.yml
echo   "%~f0"
echo   (that folder must contain or receive the Vibe-Budgeting subfolder for zip-based updates.)
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-vibe-server.ps1" -BaseDir "%CD%"

echo.
echo Finished. Press any key to close.
pause >nul
