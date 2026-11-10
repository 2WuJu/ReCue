param([ValidateSet('all', 'api', 'worker')][string]$Service = 'all')
. "$PSScriptRoot/common.ps1"
$ReCueData = if ($env:RECUE_DATA_DIR) { [IO.Path]::GetFullPath($env:RECUE_DATA_DIR) } else { Join-Path $ReCueRoot 'data' }
$ReCueRuntime = Join-Path $ReCueData 'runtime'
if (-not (Test-Path -LiteralPath $ReCueRuntime)) { Write-Output 'No service runtime found.'; return }
$ReCueMarker = Join-Path $ReCueRuntime "$Service.stop"
[IO.File]::WriteAllText($ReCueMarker, 'stop')
Write-Output "Stop requested for launcher '$Service'. The start terminal will report exit. Use the same -Service value as start.ps1."
