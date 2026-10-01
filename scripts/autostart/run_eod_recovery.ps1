<#
.SYNOPSIS
  EOD-recovery launcher for the ActivTrades DEMO trader (Task Scheduler entry point of AutoTrader-EodRecovery).

.DESCRIPTION
  Sets the working directory to THIS checkout, takes a named mutex (one launcher per artifacts dir), then runs
  scripts\autostart\eod_recovery.py with `uv run --frozen`. All policy (healthy-runner check, flatten-only runner launch, bounded retries,
  CRITICAL alerts, exit codes 0/2/9/10/20) lives in eod_recovery.py. See docs\AUTOSTART.md.

  NEVER starts/stops/touches MetaTrader 5. NEVER kills a process. The launched runner only ever flattens (reduce-only), never opens.

.PARAMETER DryRun
  Print the exact command + mutex name and exit 0 without launching anything.
#>
[CmdletBinding()]
param(
    [string]$ArtifactsDir = 'artifacts\demo_100k',
    [string]$AccountPhase = 'ALPHA_EXECUTION_DISCOVERY',
    [double]$RetrySeconds = 30,
    [string]$RetryUntil = '23:30',
    [string]$Uv = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $RepoRoot

if ([System.IO.Path]::IsPathRooted($ArtifactsDir)) { $Artifacts = $ArtifactsDir }
else { $Artifacts = Join-Path $RepoRoot $ArtifactsDir }

if (-not $Uv) {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { $Uv = $cmd.Source } else { $Uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
}

$LogDir = Join-Path $Artifacts 'logs'
$Launcher = Join-Path $LogDir 'eod_recovery_launcher.log'
function Write-Launcher([string]$Message) {
    $line = '{0} [EOD-LAUNCHER] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        Add-Content -LiteralPath $Launcher -Value $line -Encoding UTF8
    }
}

$RecArgs = @('run', '--frozen', 'python', 'scripts\autostart\eod_recovery.py',
    '--artifacts', $Artifacts, '--account-phase', $AccountPhase,
    '--retry-s', "$RetrySeconds", '--retry-until', $RetryUntil)

$sha = [System.Security.Cryptography.SHA1]::Create()
$hash = ([System.BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Artifacts.ToLowerInvariant())))).Replace('-', '').Substring(0, 12)
$MutexName = "Global\AutoTrader-EodRecovery-$hash"

if ($DryRun) {
    Write-Host "DRY RUN (nothing launched, no mutex taken, no files written)"
    Write-Host "  working directory : $RepoRoot"
    Write-Host "  mutex             : $MutexName"
    Write-Host "  launcher log      : $Launcher"
    Write-Host "  command           : `"$Uv`" $($RecArgs -join ' ')"
    exit 0
}

$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$owned = $false
try {
    try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) {
        Write-Launcher "another EOD-recovery launcher already holds $MutexName - not starting a second one. Exit 0"
        exit 0
    }
    Write-Launcher "start repo=$RepoRoot artifacts=$Artifacts uv=$Uv"
    & $Uv @RecArgs
    $code = $LASTEXITCODE
    Write-Launcher "eod_recovery exit code $code"
    exit $code
}
finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
