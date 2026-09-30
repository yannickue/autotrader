<#
.SYNOPSIS
  Read-only status: scheduled task, locks, heartbeat verdict, watchdog alert, newest log.
#>
[CmdletBinding()]
param([string]$ArtifactsDir = 'artifacts\demo_100k', [string]$TaskName = 'AutoTrader-DemoDaily')
$ErrorActionPreference = 'Continue'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if ([System.IO.Path]::IsPathRooted($ArtifactsDir)) { $art = $ArtifactsDir } else { $art = Join-Path $RepoRoot $ArtifactsDir }
Write-Host "== scheduled task '$TaskName'"
$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($t) {
    $i = Get-ScheduledTaskInfo -TaskName $TaskName
    "state=$($t.State) next=$($i.NextRunTime) last=$($i.LastRunTime) lastResult=$($i.LastTaskResult)"
} else { 'not registered' }
foreach ($n in 'runner.lock', 'supervisor.lock', 'STOP', 'watchdog_alert.json', 'watchdog_state.json') {
    $p = Join-Path $art $n
    Write-Host "== $n"
    if (Test-Path -LiteralPath $p) { Get-Content -LiteralPath $p -Raw } else { '(absent)' }
}
Write-Host "== runner --status (heartbeat verdict; exit 3 = NOT RUNNING)"
Set-Location -LiteralPath $RepoRoot
& uv run --frozen python scripts\demo_trader.py --status --artifacts $art
Write-Host "== newest log"
Get-ChildItem -LiteralPath (Join-Path $art 'logs') -Filter 'trader_*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
    ForEach-Object { $_.FullName; Get-Content -LiteralPath $_.FullName -Tail 15 }
