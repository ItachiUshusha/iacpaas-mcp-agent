$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$localTemp = Join-Path $PSScriptRoot '.venv\setup-tmp'
New-Item -ItemType Directory -Force -Path $localTemp | Out-Null
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
try {
    $env:TEMP = $localTemp
    $env:TMP = $localTemp
    if (Test-Path -LiteralPath $bundledPython) {
        & $bundledPython -m venv "$PSScriptRoot\.venv"
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv "$PSScriptRoot\.venv"
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv "$PSScriptRoot\.venv"
    } else { throw 'Python 3.10 or newer is required.' }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment.' }
    & $projectPython -m pip install -r "$PSScriptRoot\requirements-lock.txt"
    if ($LASTEXITCODE -ne 0) { throw 'Could not install project dependencies.' }
} finally {
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
}
Write-Output 'Setup complete. Run: .\run.ps1 smoke'
