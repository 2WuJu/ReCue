$ErrorActionPreference = 'Stop'
$ReCueRoot = Split-Path $PSScriptRoot -Parent
function Get-ReCueUv {
    $ReCueCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
    if ($ReCueCommand) { return $ReCueCommand.Source }
    $ReCueUserUv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $ReCueUserUv) { return $ReCueUserUv }
    throw 'uv missing. Install uv from https://docs.astral.sh/uv/getting-started/installation/ and rerun scripts/setup.ps1.'
}
function Assert-ReCueEnvironment {
    if (-not (Test-Path -LiteralPath (Join-Path $ReCueRoot '.venv\Scripts\python.exe'))) {
        throw 'Python environment missing. Run scripts/setup.ps1 first; uv manages Python and dependencies.'
    }
}
