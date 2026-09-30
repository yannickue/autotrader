<#
.SYNOPSIS
  Operator stop: create (or with -Clear remove) <artifacts>\STOP.
.DESCRIPTION
  The runner shuts down orderly when STOP exists (it checks it each cycle) and the supervisor never
  (re)starts a runner while STOP exists. The STOP file is NOT removed automatically: run with -Clear
  before the next trading day, otherwise tomorrow's 08:30 start is (intentionally) blocked.
  Never touches MT5.
#>
[CmdletBinding()]
param([string]$ArtifactsDir = 'artifacts\demo_100k', [switch]$Clear)
$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if ([System.IO.Path]::IsPathRooted($ArtifactsDir)) { $art = $ArtifactsDir } else { $art = Join-Path $RepoRoot $ArtifactsDir }
$stop = Join-Path $art 'STOP'
if ($Clear) {
    if (Test-Path -LiteralPath $stop) { Remove-Item -LiteralPath $stop -Force; Write-Host "removed $stop" } else { Write-Host "no STOP file at $stop" }
    exit 0
}
New-Item -ItemType Directory -Force -Path $art | Out-Null
Set-Content -LiteralPath $stop -Value ("operator stop {0}" -f (Get-Date -Format 's')) -Encoding ASCII
Write-Host "created $stop - the runner stops orderly; no restart until you run: stop_trader.ps1 -Clear"
