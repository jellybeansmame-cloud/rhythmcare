# Windowsのタスクスケジューラに「毎日あすけん同期」を登録する
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python).Source
$action = New-ScheduledTaskAction -Execute $python -Argument "-u `"$scriptDir\sync_day.py`" --connect --push" -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -Daily -At "22:00"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "RhythmCare-AsukenSync" -Action $action -Trigger $trigger -Settings $settings -Description "あすけんからリズムケアへ1日分を同期"
Write-Host "登録しました: 毎日 22:00 に実行"
Write-Host "注意: start-chrome.ps1 で起動したChromeがログイン済みである必要があります"
Write-Host "削除する場合: Unregister-ScheduledTask -TaskName RhythmCare-AsukenSync -Confirm:`$false"
