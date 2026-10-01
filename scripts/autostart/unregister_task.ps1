<#
.SYNOPSIS
  Remove the AutoTrader-DemoDaily and AutoTrader-EodRecovery scheduled tasks (does not stop a running trader, does not touch MT5).
#>
[CmdletBinding()]
param([string]$TaskName = 'AutoTrader-DemoDaily', [string]$EodTaskName = 'AutoTrader-EodRecovery', [switch]$DryRun)
$ErrorActionPreference = 'Stop'
foreach ($name in @($TaskName, $EodTaskName)) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "task '$name' is not registered."; continue }
    if ($DryRun) { Write-Host "DRY RUN: would run Unregister-ScheduledTask -TaskName '$name' -Confirm:`$false"; continue }
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "task '$name' unregistered. A running supervisor/runner/recovery is NOT stopped (use stop_trader.ps1)."
}
