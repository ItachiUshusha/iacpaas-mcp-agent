param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ClientArguments)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) { throw 'Run setup.ps1 first.' }
if ($ClientArguments.Count -eq 0) { throw 'Expected: agent, serve, smoke, test, or doctor.' }
$operation = $ClientArguments[0]
$remaining = @()
if ($ClientArguments.Count -gt 1) { $remaining = $ClientArguments[1..($ClientArguments.Count - 1)] }
switch ($operation) {
    'agent' { & $projectPython -X utf8 "$PSScriptRoot\agent.py" @remaining }
    'serve' { & $projectPython -X utf8 "$PSScriptRoot\server.py" @remaining }
    'smoke' { & $projectPython -X utf8 "$PSScriptRoot\smoke.py" @remaining }
    'doctor' { & $projectPython -X utf8 "$PSScriptRoot\doctor.py" @remaining }
    'test' {
        Push-Location $PSScriptRoot
        try { & $projectPython -X utf8 -m unittest discover -s tests -v } finally { Pop-Location }
    }
    default { throw 'Expected: agent, serve, smoke, test, or doctor.' }
}
exit $LASTEXITCODE
