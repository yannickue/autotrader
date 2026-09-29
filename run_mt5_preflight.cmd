@echo off
REM Double-click-safe launcher for the MT5/ActivTrades preflight check.
REM See run_mt5_preflight.ps1 for the full explanation of why this exists.
REM No credential entry required -- reads your existing local .env.

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_mt5_preflight.ps1"
