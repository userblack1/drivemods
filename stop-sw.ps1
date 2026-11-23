param([switch]$Force)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$swPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$swData = & $swPython -c 'from firmware_bot.config import Settings; print(Settings.load().data_dir)'
if ($LASTEXITCODE -ne 0) { throw 'Cannot read the firmware bot data directory.' }
$swMarkerPath = Join-Path $swData 'bot-process.json'
if (-not (Test-Path -LiteralPath $swMarkerPath)) {
    Write-Host 'No local NextMode SW process is recorded.'
    exit 0
}
$swMarker = Get-Content -LiteralPath $swMarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($swMarker.location -ne 'local') { throw 'This marker does not describe a local process.' }
$swRootProcess = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$swMarker.pid)
if ($swRootProcess) {
    if ($swRootProcess.CommandLine -notmatch '(?:^|\s)-m\s+firmware_bot\s+run(?:\s|$)') {
        throw 'The recorded PID is not the NextMode SW process.'
    }
    if ($swMarker.started_utc -is [DateTime]) {
        $swExpectedTime = $swMarker.started_utc.ToUniversalTime()
    } else {
        $swExpectedTime = [DateTimeOffset]::Parse([string]$swMarker.started_utc).UtcDateTime
    }
    if ([Math]::Abs(($swRootProcess.CreationDate.ToUniversalTime() - $swExpectedTime).TotalSeconds) -gt 3) {
        throw 'The recorded PID was reused; refusing to stop it.'
    }
    if (-not $Force) {
        $swQueueCheck = @'
import sqlite3
from firmware_bot.config import Settings
with sqlite3.connect(Settings.load().data_dir/'requests.sqlite3') as c:
    print(c.execute("SELECT COUNT(*) FROM requests WHERE state IN ('queued','searching','delivering')").fetchone()[0])
'@
        $swActive = & $swPython -c $swQueueCheck
        if ($LASTEXITCODE -ne 0 -or [int]$swActive -gt 0) { throw 'Requests are still active. Wait for completion before stopping.' }
    }
    function Stop-SwProcessTree([int]$TaskProcessId) {
        $swChildren = @(Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $TaskProcessId))
        foreach ($swChild in $swChildren) { Stop-SwProcessTree -TaskProcessId $swChild.ProcessId }
        Stop-Process -Id $TaskProcessId -Force -ErrorAction SilentlyContinue
    }
    Stop-SwProcessTree -TaskProcessId $swRootProcess.ProcessId
}
Remove-Item -LiteralPath $swMarkerPath
Write-Host 'Local NextMode SW process stopped.'
