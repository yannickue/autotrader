<#
.SYNOPSIS
  Register (or dry-run) the Task Scheduler tasks AutoTrader-DemoDaily AND AutoTrader-EodRecovery for THIS checkout.

.DESCRIPTION
  Renders scripts\autostart\AutoTrader-DemoDaily.task.xml (current user, this checkout, Mon-Fri 08:30 local time, repeating
  every 15 min) and scripts\autostart\AutoTrader-EodRecovery.task.xml (Lane R: independent flatten-only EOD recovery, Mon-Fri
  21:45 / 21:50 / 21:55 / 22:00 then every 2 min until 22:30 local time), validates BOTH through the Task Scheduler COM parser
  WITHOUT registering, and prints the exact registration command. Only without -DryRun/-WhatIf are the tasks registered
  (run by the lead).

  Runs as the current user, only while logged on (interactive MT5 session). Never touches MT5.

.PARAMETER DryRun
  Validate + print only. -WhatIf behaves identically.
.PARAMETER WakeToRun
  Let the tasks wake the PC from sleep (default off; see docs\AUTOSTART.md).
.PARAMETER Force
  Replace an already registered task of the same name.
.PARAMETER OutXml
  Also write the rendered DemoDaily XML to this path (for review); the EOD-recovery XML goes to <OutXml>.eod.xml.
.PARAMETER Disabled
  Register the tasks but leave them DISABLED (Disable-ScheduledTask right after registration): for technical validation without letting
  any trigger fire. Enable later with enable_task.ps1 (the lead, at the final controlled deployment).
.PARAMETER SkipEodRecovery
  Handle only AutoTrader-DemoDaily (NOT recommended: the 15-min repetition alone is not a sufficient EOD defence).
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'AutoTrader-DemoDaily',
    [string]$EodTaskName = 'AutoTrader-EodRecovery',
    [switch]$WakeToRun,
    [switch]$DryRun,
    [switch]$WhatIf,
    [switch]$Force,
    [switch]$SkipEodRecovery,
    [switch]$Disabled,
    [string]$OutXml = '',
    [string]$UserId = '',
    [string]$RepoRoot = ''
)

$ErrorActionPreference = 'Stop'
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
if (-not $UserId) { $UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name }
$dry = $DryRun.IsPresent -or $WhatIf.IsPresent
$wake = $WakeToRun.IsPresent.ToString().ToLowerInvariant()

function New-TaskXml([string]$TemplateFile, [string]$DefaultName, [string]$Name) {
    $template = Get-Content -LiteralPath (Join-Path $PSScriptRoot $TemplateFile) -Raw -Encoding UTF8
    $xml = $template.Replace('@@USER_ID@@', [System.Security.SecurityElement]::Escape($UserId))
    $xml = $xml.Replace('@@REPO_ROOT@@', [System.Security.SecurityElement]::Escape($RepoRoot))
    $xml = $xml.Replace('@@START_DATE@@', (Get-Date -Format 'yyyy-MM-dd')).Replace('@@WAKE@@', $wake)
    $xml = $xml.Replace("\$DefaultName</URI>", "\$Name</URI>")
    # placeholders may still appear in the explanatory XML comment; only the body matters
    $body = $xml -replace '(?s)<!--.*?-->', ''
    if ($body -match '@@') { throw "unreplaced placeholder in $TemplateFile" }
    # Validate with the Task Scheduler parser: builds an in-memory definition only, registers nothing.
    $svc = New-Object -ComObject 'Schedule.Service'
    $svc.Connect()
    $def = $svc.NewTask(0)
    $def.XmlText = $xml
    $bounds = @()
    foreach ($t in $def.Triggers) {
        $rep = ''
        if ($t.Repetition -and $t.Repetition.Interval) { $rep = " (repeat $($t.Repetition.Interval) for $($t.Repetition.Duration))" }
        $bounds += ("'{0}'{1}" -f $t.StartBoundary, $rep)
    }
    Write-Host ("[{0}] XML valid. Trigger start boundaries (local wall-clock): {1}; StartWhenAvailable={2}, MultipleInstances policy={3}, WakeToRun={4}, ExecutionTimeLimit={5}" -f $Name, ($bounds -join ', '), $def.Settings.StartWhenAvailable, $def.Settings.MultipleInstances, $def.Settings.WakeToRun, $def.Settings.ExecutionTimeLimit)
    return $xml
}

$tasks = @(@{ Name = $TaskName; Xml = (New-TaskXml 'AutoTrader-DemoDaily.task.xml' 'AutoTrader-DemoDaily' $TaskName) })
if (-not $SkipEodRecovery) {
    $tasks += @{ Name = $EodTaskName; Xml = (New-TaskXml 'AutoTrader-EodRecovery.task.xml' 'AutoTrader-EodRecovery' $EodTaskName) }
}

if ($OutXml) {
    [System.IO.File]::WriteAllText($OutXml, $tasks[0].Xml, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "rendered XML written to $OutXml"
    if (-not $SkipEodRecovery) {
        [System.IO.File]::WriteAllText("$OutXml.eod.xml", $tasks[1].Xml, (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "rendered EOD-recovery XML written to $OutXml.eod.xml"
    }
}

$extra = ''
if ($WakeToRun) { $extra += ' -WakeToRun' }
if ($Force) { $extra += ' -Force' }
if ($SkipEodRecovery) { $extra += ' -SkipEodRecovery' }
if ($Disabled) { $extra += ' -Disabled' }
if ($dry) {
    Write-Host 'DRY RUN: nothing registered.'
    if ($Disabled) { Write-Host 'The tasks would be registered DISABLED (no trigger can fire until enable_task.ps1 is run).' }
    Write-Host 'To register (lead only), run:'
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\register_task.ps1`"$extra"
    foreach ($t in $tasks) {
        Write-Host "  (which calls: Register-ScheduledTask -TaskName '$($t.Name)' -User '$UserId' -Xml <rendered xml>)"
    }
    exit 0
}

foreach ($t in $tasks) {
    $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
    if ($existing -and -not $Force) { throw "task '$($t.Name)' already exists; use -Force to replace it or unregister_task.ps1" }
}
foreach ($t in $tasks) {
    Register-ScheduledTask -TaskName $t.Name -User $UserId -Xml $t.Xml -Force:$Force | Out-Null
    Write-Host "registered '$($t.Name)' (local time, user $UserId, logged-on only)."
    if ($Disabled) {
        Disable-ScheduledTask -TaskName $t.Name | Out-Null
        Write-Host "task '$($t.Name)' is DISABLED (enable_task.ps1 enables it)."
    }
    Get-ScheduledTaskInfo -TaskName $t.Name | Format-List TaskName, NextRunTime, LastRunTime, LastTaskResult
}
