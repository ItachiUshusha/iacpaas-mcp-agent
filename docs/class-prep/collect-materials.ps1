param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ClientArguments)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
& (Join-Path $projectRoot 'run.ps1') study --verify-mcp @ClientArguments
exit $LASTEXITCODE
