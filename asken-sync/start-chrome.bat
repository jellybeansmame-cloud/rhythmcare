@echo off
setlocal
set "PROFILE=%~dp0.chrome-cdp-profile"
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
    echo Google Chrome not found.
    exit /b 1
)

echo Starting Chrome (remote debugging port 9222)
echo 1. Sign in to Asuken with Google in this window
echo 2. In another terminal: python investigate.py --connect
echo.

start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%PROFILE%" https://www.asken.jp/login
