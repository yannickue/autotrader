<#
.SYNOPSIS
  Register (or dry-run) the Task Scheduler task AutoTrader-DemoDaily for THIS checkout.

.DESCRIPTION
  Renders scripts\autostart\AutoTrader-DemoDaily.task.xml (current user, this checkout, Mon-Fri 08:30
  local time), validates it through the Task Scheduler COM parser WITHOUT registering, and prints the
  exact registration command. Only without -DryRun/-WhatIf is the task registered (run by the lead).

  Runs as the current user, only while logged on (interactive MT5 session). Never touches MT5.

.PARAMETER DryRun
  Validate + print only. -WhatIf behaves identically.
.PARAMETER WakeToRun
  Let the task wake the PC from sleep for the 08:30 trigger (default off; see docs\AUTOSTART.md).
.PARAMETER Force
  Replace an already registered task of the same name.
.PARAMETER OutXml
  Also write the rendered XML to this path (for review).
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'AutoTrader-DemoDaily',
    [switch]$WakeToRun,
    [switch]$DryRun,
    [switch]$WhatIf,
    [switch]$Force,
    [string]$OutXml = '',
    [string]$UserId = '',
    [string]$RepoRoot = ''
)

$ErrorActionPreference = 'Stop'
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
if (-not $UserId) { $UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name }
$dry = $DryRun.IsPresent -or $WhatIf.IsPresent

$template = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'AutoTrader-DemoDaily.task.xml') -Raw -Encoding UTF8
$wake = $WakeToRun.IsPresent.ToString().ToLowerInvariant()
$xml = $template.Replace('@@USER_ID@@', [System.Security.SecurityElement]::Escape($UserId))
$xml = $xml.Replace('@@REPO_ROOT@@', [System.Security.SecurityElement]::Escape($RepoRoot))
$xml = $xml.Replace('@@START_DATE@@', (Get-Date -Format 'yyyy-MM-dd')).Replace('@@WAKE@@', $wake)
$xml = $xml.Replace('\AutoTrader-DemoDaily</URI>', "\$TaskName</URI>")
# placeholders may still appear in the explanatory XML comment; only the body matters
$body = $xml -replace '(?s)<!--.*?-->', ''
if ($body -match '@@') { throw 'unreplaced placeholder in task XML' }

# Validate with the Task Scheduler parser: builds an in-memory definition only, registers nothing.
$svc = New-Object -ComObject 'Schedule.Service'
$svc.Connect()
$def = $svc.NewTask(0)
$def.XmlText = $xml
$trig = $def.Triggers.Item(1)
Write-Host ("XML valid. Trigger start boundary '{0}' (local wall-clock), StartWhenAvailable={1}, MultipleInstances policy={2}, WakeToRun={3}, ExecutionTimeLimit={4}" -f $trig.StartBoundary, $def.Settings.StartWhenAvailable, $def.Settings.MultipleInstances, $def.Settings.WakeToRun, $def.Settings.ExecutionTimeLimit)

if ($OutXml) {
    [System.IO.File]::WriteAllText($OutXml, $xml, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "rendered XML written to $OutXml"
}

$extra = ''
if ($WakeToRun) { $extra += ' -WakeToRun' }
if ($Force) { $extra += ' -Force' }
if ($dry) {
    Write-Host 'DRY RUN: nothing registered.'
    Write-Host 'To register (lead only), run:'
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\register_task.ps1`"$extra"
    Write-Host "  (which calls: Register-ScheduledTask -TaskName '$TaskName' -User '$UserId' -Xml <rendered xml>)"
    exit 0
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $Force) { throw "task '$TaskName' already exists; use -Force to replace it or unregister_task.ps1" }
Register-ScheduledTask -TaskName $TaskName -User $UserId -Xml $xml -Force:$Force | Out-Null
Write-Host "registered '$TaskName' (Mon-Fri 08:30 local, user $UserId, logged-on only)."
Get-ScheduledTaskInfo -TaskName $TaskName | Format-List TaskName, NextRunTime, LastRunTime, LastTaskResult
