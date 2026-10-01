<#
.SYNOPSIS
  Enable the AutoTrader scheduled tasks (AutoTrader-DemoDaily and AutoTrader-EodRecovery). LEAD ONLY, at the final controlled deployment.

.DESCRIPTION
  Enable-ScheduledTask on both tasks (or -Only one of them). Does not start anything by itself and does not touch MT5. The normal task
  additionally needs <artifacts>\deploy_approved.json (approve_deploy.ps1) before its supervisor launches the runner.

.PARAMETER Only
  'Daily' or 'EodRecovery': act on one task only.
.PARAMETER DryRun
  Print what would be enabled.
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
    if ($DryRun) { Write-Host "DRY RUN: would run Enable-ScheduledTask -TaskName '$name' (current state: $($t.State))"; continue }
    Enable-ScheduledTask -TaskName $name | Out-Null
    Write-Host "task '$name' ENABLED (state: $((Get-ScheduledTask -TaskName $name).State))."
}
if ($Only -eq 'Daily') {
    # The independent EOD recovery is the zero-overnight safety net of the trader: it MUST stay enabled whenever the daily task runs.
    $eod = Get-ScheduledTask -TaskName $EodTaskName -ErrorAction SilentlyContinue
    if (-not $eod) {
        Write-Warning "WARNING: -Only Daily left '$EodTaskName' UNREGISTERED. The EOD recovery task must exist and stay ENABLED (zero-overnight safety net)."
    } elseif ($eod.State -eq 'Disabled') {
        Write-Warning "WARNING: -Only Daily left '$EodTaskName' DISABLED. The EOD recovery task must stay ENABLED (zero-overnight safety net) - run enable_task.ps1 -Only EodRecovery."
    }
}
