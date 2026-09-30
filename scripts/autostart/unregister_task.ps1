<#
.SYNOPSIS
  Remove the AutoTrader-DemoDaily scheduled task (does not stop a running trader, does not touch MT5).
#>
[CmdletBinding()]
param([string]$TaskName = 'AutoTrader-DemoDaily', [switch]$DryRun)
$ErrorActionPreference = 'Stop'
$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $t) { Write-Host "task '$TaskName' is not registered."; exit 0 }
if ($DryRun) { Write-Host "DRY RUN: would run Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"; exit 0 }
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "task '$TaskName' unregistered. A running supervisor/runner is NOT stopped (use stop_trader.ps1)."
