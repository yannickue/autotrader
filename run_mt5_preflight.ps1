# Double-click-safe launcher for the MT5/ActivTrades preflight check.
#
# Use this if running `uv run python scripts/mt5_preflight.py` from an
# automation shell (e.g. an AI coding assistant's terminal) fails with an
# MT5 IPC error even though the terminal is running -- that pattern usually
# means the automation shell's processes lack access to your interactive
# Windows desktop/window-station, which the MT5 Python API needs to reach
# the terminal GUI. Running this script directly on your own desktop
# (double-click, or from a PowerShell window you opened yourself) uses your
# real interactive session, so it doesn't hit that limitation.
#
# No credentials are entered here -- this only loads your existing local
# `.env` (see .env.example) via the project's own config loader.
#
# Usage: double-click this file, or right-click -> "Run with PowerShell".

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "MT5 preflight: repository $PSScriptRoot" -ForegroundColor Cyan

if (-not (Test-Path ".env")) {
    Write-Host ""
    Write-Host "No .env file found at the repository root." -ForegroundColor Yellow
    Write-Host "Copy .env.example to .env and fill in your ActivTrades DEMO credentials first."
    Write-Host ""
    Read-Host "Press Enter to close"
    exit 2
}

Write-Host "Running: uv run python scripts/mt5_preflight.py" -ForegroundColor Cyan
Write-Host ""

& uv run python scripts/mt5_preflight.py
$exitCode = $LASTEXITCODE

Write-Host ""
Write-Host "Exit code: $exitCode" -ForegroundColor $(if ($exitCode -eq 0) { "Green" } else { "Yellow" })
Write-Host ""
Read-Host "Press Enter to close this window"
exit $exitCode
