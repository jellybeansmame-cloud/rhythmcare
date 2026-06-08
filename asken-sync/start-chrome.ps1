# Launch Chrome for Google login, then run: python investigate.py --connect
$profile = Join-Path $PSScriptRoot ".chrome-cdp-profile"
$chrome = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"

if (-not (Test-Path -LiteralPath $chrome)) {
    $pf86 = ${env:ProgramFiles(x86)}
    $chrome = Join-Path $pf86 "Google\Chrome\Application\chrome.exe"
}

if (-not (Test-Path -LiteralPath $chrome)) {
    Write-Error "Google Chrome not found."
    exit 1
}

Write-Host "Starting Chrome (remote debugging port 9222)"
Write-Host "1. Sign in to Asuken with Google in this window"
Write-Host "2. In another terminal: python sync_day.py --connect --upload-cookies"
Write-Host ""

$args = @(
    "--remote-debugging-port=9222",
    "--user-data-dir=$profile",
    "https://www.asken.jp/login"
)
Start-Process -FilePath $chrome -ArgumentList $args
