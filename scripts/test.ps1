# Thin wrapper: .\scripts\test.ps1 fast|integration|safety|slow|full|changed [args]
# Equivalent to: uv run python scripts/run_tests.py <cmd> [args]
param([Parameter(Mandatory = $true, Position = 0)][ValidateSet('fast','integration','safety','slow','full','changed')][string]$Cmd,
      [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try { uv run python scripts/run_tests.py $Cmd @Rest; exit $LASTEXITCODE } finally { Pop-Location }
