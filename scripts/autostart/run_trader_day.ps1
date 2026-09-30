<#
.SYNOPSIS
  Daily launcher for the ActivTrades DEMO trader (Task Scheduler entry point).

.DESCRIPTION
  Sets the working directory to THIS checkout, takes a named mutex (one launcher per artifacts dir),
  then runs the bounded supervisor (scripts\autostart\supervisor.py) with the production arguments via
  `uv run --frozen`. All policy (operating-day guard, STOP file, healthy-runner check, restart budget,
  backoff, alerts, dated log scripts/.../logs/trader_YYYYMMDD_HHMMSS.log) lives in supervisor.py.

  NEVER starts/stops/touches MetaTrader 5. NEVER kills the runner. See docs\AUTOSTART.md.

.PARAMETER DryRun
  Print the exact supervisor command + mutex name and exit 0 without launching anything.
#>
[CmdletBinding()]
param(
    [string]$ArtifactsDir = 'artifacts\demo_100k',
    [string]$AccountPhase = 'ALPHA_EXECUTION_DISCOVERY',
    [ValidateSet('auto', 'on', 'off')][string]$Daily = 'auto',
    [int]$MaxRestartsPerDay = 8,
    [string]$BackoffSeconds = '30,60,120,300,600',
    [string]$EndOfDay = '22:15',
    [string]$Uv = '',
    [switch]$IgnoreOperatingDay,
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
$Launcher = Join-Path $LogDir 'autostart_launcher.log'
function Write-Launcher([string]$Message) {
    $line = '{0} [LAUNCHER] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        Add-Content -LiteralPath $Launcher -Value $line -Encoding UTF8
    }
}

$SupArgs = @('run', '--frozen', 'python', 'scripts\autostart\supervisor.py',
    '--artifacts', $Artifacts, '--account-phase', $AccountPhase, '--daily', $Daily,
    '--max-restarts-per-day', "$MaxRestartsPerDay", '--backoff', $BackoffSeconds,
    '--end-of-day', $EndOfDay)
if ($IgnoreOperatingDay) { $SupArgs += '--ignore-operating-day' }

# One launcher per artifacts dir (the supervisor adds supervisor.lock, the runner adds runner.lock).
$sha = [System.Security.Cryptography.SHA1]::Create()
$hash = ([System.BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Artifacts.ToLowerInvariant())))).Replace('-', '').Substring(0, 12)
$MutexName = "Global\AutoTrader-DemoDaily-$hash"

if ($DryRun) {
    Write-Host "DRY RUN (nothing launched, no mutex taken, no files written)"
    Write-Host "  working directory : $RepoRoot"
    Write-Host "  mutex             : $MutexName"
    Write-Host "  launcher log      : $Launcher"
    Write-Host "  command           : `"$Uv`" $($SupArgs -join ' ')"
    exit 0
}

$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$owned = $false
try {
    try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) {
        Write-Launcher "another launcher already holds $MutexName - not starting a second one. Exit 0"
        exit 0
    }
    Write-Launcher "start repo=$RepoRoot artifacts=$Artifacts uv=$Uv"
    & $Uv @SupArgs
    $code = $LASTEXITCODE
    Write-Launcher "supervisor exit code $code"
    exit $code
}
finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
