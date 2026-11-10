param([ValidateSet('all', 'api', 'worker')][string]$Service = 'all')
. "$PSScriptRoot/common.ps1"
$ReCueUv = Get-ReCueUv
Assert-ReCueEnvironment
Push-Location (Join-Path $ReCueRoot 'backend')
try {
    & $ReCueUv run --offline --locked python -m app.run --service $Service
    if ($LASTEXITCODE -ne 0) { throw 'ReCue stopped with an error. See output above.' }
} finally { Pop-Location }
