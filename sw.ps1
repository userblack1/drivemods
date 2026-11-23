param(
    [ValidateSet('check', 'connect', 'run', 'reset-bridge')][string]$Command = 'check',
    [switch]$Online,
    [switch]$SourceDialogChecked
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    throw 'Требуется Python 3.12 и окружение .venv. См. docs/firmware-bot.md.'
}
$swArguments = @('-m', 'firmware_bot', $Command)
if ($Online) { $swArguments += '--online' }
if ($SourceDialogChecked) { $swArguments += '--source-dialog-checked' }
& .\.venv\Scripts\python.exe @swArguments
exit $LASTEXITCODE
