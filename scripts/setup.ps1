$ErrorActionPreference = 'Stop'
& node (Join-Path $PSScriptRoot 'recue.mjs') setup
if ($LASTEXITCODE -ne 0) { throw 'ReCue setup failed. See output above.' }
