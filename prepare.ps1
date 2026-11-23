param([switch]$Offline)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    $python312 = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (Test-Path -LiteralPath $python312) {
        & $python312 -m venv .venv
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw 'Требуется Python 3.12 x64.' }
        & $pythonCommand.Source -c 'import sys; assert sys.version_info >= (3, 12)'
        if ($LASTEXITCODE -ne 0) { throw 'Требуется рабочий Python 3.12 или новее.' }
        & $pythonCommand.Source -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать .venv.' }
}

if ((Test-Path -LiteralPath 'wheelhouse') -or $Offline) {
    if (-not (Test-Path -LiteralPath 'wheelhouse')) { throw 'Папка wheelhouse отсутствует: офлайн-установка невозможна.' }
    & .\.venv\Scripts\python.exe -c 'import sys; assert sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32'
    if ($LASTEXITCODE -ne 0) { throw 'Офлайн-комплект собран для Python 3.12 x64. Нужна именно эта версия окружения.' }
    & .\.venv\Scripts\python.exe -m pip install --disable-pip-version-check --no-index --find-links wheelhouse -r requirements.lock
} else {
    & .\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.lock
}
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости.' }
if (-not (Test-Path -LiteralPath '.env')) {
    & .\.venv\Scripts\python.exe scripts/setup_dev_env.py
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось безопасно создать конфигурацию разработки.' }
}
& .\.venv\Scripts\python.exe -m drivemods_bot doctor
if ($LASTEXITCODE -ne 0) { throw 'Локальная проверка не пройдена.' }
Write-Host 'Подготовка завершена. Секреты и разрешения на загрузку/покупку не изменялись.'

