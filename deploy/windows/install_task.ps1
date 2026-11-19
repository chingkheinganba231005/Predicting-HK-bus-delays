# Registers a scheduled task that starts the collector at logon and restarts
# it if it stops. Run from the repository root in PowerShell:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1
# Remove it later with:
#   Unregister-ScheduledTask -TaskName "HK bus ETA collector" -Confirm:$false

$repo = (Get-Location).Path
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Create the virtual environment first (see docs/collecting.md)" }

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m hkbus.collector --db data\collector.sqlite --log-dir data\logs" `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 60) -StartWhenAvailable
Register-ScheduledTask -TaskName "HK bus ETA collector" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Polls KMB and Citybus ETA APIs every minute" -Force
Start-ScheduledTask -TaskName "HK bus ETA collector"
Write-Host "Collector task registered and started. Logs: $repo\data\logs\collector.log"
