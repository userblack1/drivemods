$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) { throw 'Сначала выполните prepare.ps1.' }

# Every self-test uses a separate local directory and has store writes disabled.
$previousMode = $env:APP_MODE
$previousDataDir = $env:DATA_DIR
$previousUploads = $env:STORE_UPLOADS_ENABLED
$previousPurchases = $env:STORE_PURCHASES_ENABLED
try {
    $env:APP_MODE = 'demo'
    $env:DATA_DIR = Join-Path $PSScriptRoot 'data\selftest'
    $env:STORE_UPLOADS_ENABLED = 'false'
    $env:STORE_PURCHASES_ENABLED = 'false'
    & .\.venv\Scripts\python.exe -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Проверки не пройдены.' }
    & .\.venv\Scripts\python.exe -m drivemods_bot demo
    if ($LASTEXITCODE -ne 0) { throw 'Демонстрация не завершилась.' }
    & .\.venv\Scripts\python.exe -m drivemods_bot doctor
    if ($LASTEXITCODE -ne 0) { throw 'Проверка окружения не пройдена.' }
} finally {
    $env:APP_MODE = $previousMode
    $env:DATA_DIR = $previousDataDir
    $env:STORE_UPLOADS_ENABLED = $previousUploads
    $env:STORE_PURCHASES_ENABLED = $previousPurchases
}
Write-Host 'Проверки завершены без обращений к платным операциям DriveMods.'

