# PowerShell script to register AI Job Hunter as a persistent Windows Scheduled Task
# This ensures the worker runs continuously and automatically restarts if rebooted.

$TaskName = "AIJobHunterPersistentWorker"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectDir = Resolve-Path "$ScriptDir\.."
$PythonExe = (Get-Command python.exe).Source

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "agent\main.py --worker" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Write-Host "Registering Scheduled Task: $TaskName..."
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "AI Job Hunter Real-Time Persistent Worker" -Force
Write-Host "Task successfully registered! Starting task..."
Start-ScheduledTask -TaskName $TaskName
Write-Host "Worker is now running in the background."
