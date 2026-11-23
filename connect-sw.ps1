param([string]$AccountLabel = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$Host.UI.RawUI.WindowTitle = 'NextMode SW - Telegram account connection'
if ($AccountLabel) {
    $Host.UI.RawUI.WindowTitle = 'NextMode SW - Connect ' + $AccountLabel
    Write-Host ('ACCOUNT TO CONNECT: ' + $AccountLabel)
}
Write-Host 'NextMode SW: sign in with the account that has access to DriveMods SW.'
Write-Host 'Enter the phone number, then the Telegram login code, then 2FA password if requested.'
Write-Host 'The code and password are hidden while typing. Do not send them in the chat.'
Write-Host ''
& .\.venv\Scripts\python.exe -m firmware_bot connect
$swConnectExitCode = $LASTEXITCODE
Write-Host ''
if ($swConnectExitCode -eq 0) {
    Write-Host 'Connected. Return to the chat and say: connected.'
} else {
    Write-Host 'Connection failed. Keep this window open and tell the assistant about the error.'
}
Read-Host 'Press Enter to close this window'
