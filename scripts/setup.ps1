. "$PSScriptRoot/common.ps1"
$ReCueUv = Get-ReCueUv
Push-Location $ReCueRoot
try {
    & $ReCueUv python install
    if ($LASTEXITCODE -ne 0) { throw 'uv could not install the Python version pinned in .python-version.' }
    & $ReCueUv sync --locked
    if ($LASTEXITCODE -ne 0) { throw 'uv dependency synchronization failed. See output above.' }
    Push-Location frontend
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
} finally { Pop-Location }
