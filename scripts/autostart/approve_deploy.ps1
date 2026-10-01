<#
.SYNOPSIS
  Approve (or -Revoke / -Check) the deployment of THIS checkout for the NORMAL daily task. LEAD ONLY, at the final controlled deployment.

.DESCRIPTION
  Writes <artifacts>\deploy_approved.json = {"sha": <git rev-parse HEAD>, "approved_utc": ...}. The supervisor of AutoTrader-DemoDaily
  refuses to start the runner unless that sha equals HEAD of this checkout and tracked files are clean (see scripts\autostart\deploy_gate.py).
  A dirty checkout cannot be approved. The EOD recovery (flatten-only) is never gated by this file.

.PARAMETER Revoke
  Delete the approval file.
.PARAMETER Check
  Only print the gate verdict (exit 30 = not approved).
.PARAMETER DryRun
  Print the command, do nothing.
#>
[CmdletBinding()]
param(
    [string]$ArtifactsDir = 'artifacts\demo_100k',
    [switch]$Revoke,
    [switch]$Check,
    [string]$Uv = '',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $RepoRoot
if ([System.IO.Path]::IsPathRooted($ArtifactsDir)) { $Artifacts = $ArtifactsDir } else { $Artifacts = Join-Path $RepoRoot $ArtifactsDir }
if (-not $Uv) {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { $Uv = $cmd.Source } else { $Uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
}
$mode = '--approve'
if ($Revoke) { $mode = '--revoke' } elseif ($Check) { $mode = '--check' }
$GateArgs = @('run', '--frozen', 'python', 'scripts\autostart\deploy_gate.py', '--artifacts', $Artifacts, $mode)
if ($DryRun) {
    Write-Host "DRY RUN (nothing executed)"
    Write-Host "  command : `"$Uv`" $($GateArgs -join ' ')"
    exit 0
}
& $Uv @GateArgs
exit $LASTEXITCODE
