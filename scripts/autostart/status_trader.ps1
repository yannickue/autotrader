<#
.SYNOPSIS
  Read-only status: scheduled task, locks, heartbeat verdict, watchdog alert, newest log.
#>
[CmdletBinding()]
param([string]$ArtifactsDir = 'artifacts\demo_100k', [string]$TaskName = 'AutoTrader-DemoDaily', [string]$EodTaskName = 'AutoTrader-EodRecovery')
$ErrorActionPreference = 'Continue'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if ([System.IO.Path]::IsPathRooted($ArtifactsDir)) { $art = $ArtifactsDir } else { $art = Join-Path $RepoRoot $ArtifactsDir }
Write-Host "NOTE: both tasks run with LogonType InteractiveToken = only while the user is LOGGED ON. After a reboot with no logon NO task runs (including the EOD recovery): positions would stay open overnight protected only by broker stops. See docs\AUTOSTART.md."
foreach ($tn in @($TaskName, $EodTaskName)) {
    Write-Host "== scheduled task '$tn'"
    $t = Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue
    if ($t) {
        $i = Get-ScheduledTaskInfo -TaskName $tn
        "state=$($t.State) next=$($i.NextRunTime) last=$($i.LastRunTime) lastResult=$($i.LastTaskResult)"
    } else { 'not registered' }
}
foreach ($n in 'runner.lock', 'supervisor.lock', 'eod_recovery.lock', 'deploy_approved.json', 'STOP', 'watchdog_alert.json', 'watchdog_state.json') {
    $p = Join-Path $art $n
    Write-Host "== $n"
    if (Test-Path -LiteralPath $p) { Get-Content -LiteralPath $p -Raw } else { '(absent)' }
}
Write-Host "== deploy gate (exit 30 = the normal task would refuse to start the runner)"
Set-Location -LiteralPath $RepoRoot
& uv run --frozen python scripts\autostart\deploy_gate.py --artifacts $art --check
Write-Host "== runner --status (heartbeat verdict; exit 3 = NOT RUNNING)"
Set-Location -LiteralPath $RepoRoot
& uv run --frozen python scripts\demo_trader.py --status --artifacts $art
Write-Host "== newest log"
Get-ChildItem -LiteralPath (Join-Path $art 'logs') -Filter 'trader_*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
    ForEach-Object { $_.FullName; Get-Content -LiteralPath $_.FullName -Tail 15 }
