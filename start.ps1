param([switch]$LocalFallback)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$serverMarker = Join-Path $PSScriptRoot 'data\server-deployment.json'
if (-not $LocalFallback -and (Test-Path -LiteralPath $serverMarker)) {
    $serverDeployment = Get-Content -LiteralPath $serverMarker -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($serverDeployment.active) {
        Write-Host 'Бот перенесён на сервер VDSka. Для проверки отправьте /status в Telegram.'
        Write-Host 'Резервный локальный запуск: start.ps1 -LocalFallback, только после остановки серверной службы.'
        exit 0
    }
}
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    throw 'Сначала выполните powershell -ExecutionPolicy Bypass -File .\prepare.ps1'
}
if (-not (Test-Path -LiteralPath '.env')) {
    & .\.venv\Scripts\python.exe scripts/setup_dev_env.py
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось безопасно создать конфигурацию разработки.' }
    Write-Host 'Создан demo .env. Для разработки используйте отдельного тестового бота и его ADMIN_IDS.'
    exit 1
}
& .\.venv\Scripts\python.exe -m drivemods_bot run
exit $LASTEXITCODE

