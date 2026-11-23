$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtimeFile = Join-Path $PSScriptRoot 'data\bot-process.json'
if (-not (Test-Path -LiteralPath $runtimeFile)) {
    Write-Host 'Запись фонового процесса не найдена.'
    exit 0
}
$runtime = Get-Content -LiteralPath $runtimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not [string]::Equals([string]$runtime.project_root, $PSScriptRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Запись процесса принадлежит другой папке проекта.'
}
$recordedProcess = Get-Process -Id ([int]$runtime.pid) -ErrorAction SilentlyContinue
if (-not $recordedProcess) {
    Write-Host 'Фоновый процесс уже завершён.'
    exit 0
}
$recordedStart = [datetime]::Parse([string]$runtime.started_at).ToUniversalTime()
if ([math]::Abs(($recordedProcess.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -gt 1) {
    throw 'PID уже принадлежит другому запуску. Остановка отменена.'
}
$expectedPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not [string]::Equals($recordedProcess.Path, $expectedPython, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Путь процесса отличается от Python этого проекта.'
}
# Windows venv uses a launcher with a child Python process. Stop only that
# explicitly matched bot command, then its recorded launcher.
$botChildren = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($recordedProcess.Id)" |
    Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '-m\s+drivemods_bot\s+run' }
foreach ($botChild in $botChildren) { Stop-Process -Id $botChild.ProcessId -ErrorAction SilentlyContinue }
Stop-Process -Id $recordedProcess.Id -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $runtimeFile
Write-Host 'Фоновый бот остановлен. Файлы заказов и настройки сохранены.'
