<#
.SYNOPSIS
  Disable the AutoTrader scheduled tasks (AutoTrader-DemoDaily and AutoTrader-EodRecovery) so no trigger can fire.

.DESCRIPTION
  Disable-ScheduledTask on both tasks (or -Only one). Does not stop a running supervisor/runner/recovery (use stop_trader.ps1) and does
  not touch MT5. NOTE: disabling AutoTrader-EodRecovery removes the independent 21:45-22:30 flatten defence - do that only while the
  trader is deliberately stopped AND the broker is known flat.

.PARAMETER Only
  'Daily' or 'EodRecovery': act on one task only.
.PARAMETER DryRun
  Print what would be disabled.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'AutoTrader-DemoDaily',
    [string]$EodTaskName = 'AutoTrader-EodRecovery',
    [ValidateSet('', 'Daily', 'EodRecovery')][string]$Only = '',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$names = @()
if ($Only -ne 'EodRecovery') { $names += $TaskName }
if ($Only -ne 'Daily') { $names += $EodTaskName }
foreach ($name in $names) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "task '$name' is not registered."; continue }
    if ($DryRun) { Write-Host "DRY RUN: would run Disable-ScheduledTask -TaskName '$name' (current state: $($t.State))"; continue }
    Disable-ScheduledTask -TaskName $name | Out-Null
    Write-Host "task '$name' DISABLED (state: $((Get-ScheduledTask -TaskName $name).State))."
}
