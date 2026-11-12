param([ValidateSet('all', 'api', 'worker')][string]$Service = 'all')
$ErrorActionPreference = 'Stop'
& node (Join-Path $PSScriptRoot 'recue.mjs') start --service $Service
if ($LASTEXITCODE -ne 0) { throw 'ReCue stopped with an error. See output above.' }
