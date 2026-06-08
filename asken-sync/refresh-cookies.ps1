# Asuken cookie refresh (double-click helper)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  RhythmCare - Asuken Cookie Update" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path "firebase_config.json")) {
    Write-Host "[ERROR] firebase_config.json not found." -ForegroundColor Red
    Write-Host "Copy firebase_config.json.example and set firebase_uid + service account path."
    Read-Host "Press Enter to exit"
    exit 1
}

try {
    $python = (Get-Command python -ErrorAction Stop).Source
}
catch {
    Write-Host "[ERROR] Python not found." -ForegroundColor Red
    Write-Host "Install from https://www.python.org/"
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "[1/2] Starting Chrome..."
Write-Host "      Log in to Asuken with Google in the Chrome window."
Write-Host ""

& "$scriptDir\start-chrome.ps1"

Write-Host ""
Write-Host "[2/2] After login, return here and press Enter..."
Read-Host | Out-Null

Write-Host ""
Write-Host "Uploading cookies..."
& $python -u "$scriptDir\sync_day.py" --connect --upload-cookies
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host "[OK] Cookie updated." -ForegroundColor Green
    Write-Host "     Reload RhythmCare settings page (F5)."
}
else {
    Write-Host "[FAILED] See error messages above." -ForegroundColor Red
}

Read-Host "Press Enter to exit"
exit $code
