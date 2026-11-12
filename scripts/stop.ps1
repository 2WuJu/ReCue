param([ValidateSet('all', 'api', 'worker')][string]$Service = 'all')
$ErrorActionPreference = 'Stop'
& node (Join-Path $PSScriptRoot 'recue.mjs') stop --service $Service
if ($LASTEXITCODE -ne 0) { throw 'ReCue stop request failed. See output above.' }
