# Registers a Windows scheduled task that starts the show player when this user logs on.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-startup-task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-startup-task.ps1 -Uninstall
#
# The player runs windowless (pythonw) and writes to logs\player.log in the repo.
# Manage it with:  Start-ScheduledTask / Stop-ScheduledTask -TaskName "HouseLights Player"

param(
    [string]$TaskName = "HouseLights Player",
    [string]$Config = "config.toml",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task '$TaskName'"
    return
}

$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source
$configPath = Join-Path $repo $Config
if (-not (Test-Path $configPath)) { throw "Config not found: $configPath (copy config.example.toml)" }
$log = Join-Path $repo "logs\player.log"

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "-m houselights serve --config `"$configPath`" --log `"$log`"" `
    -WorkingDirectory $repo

# Logon (not boot) so it runs as this user with their network access; a short delay lets Wi-Fi/network come up.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT30S"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "HouseLighting show player (https://github.com/dportwood/HouseLighting)" -Force | Out-Null

Write-Host "Registered '$TaskName': $pythonw -m houselights serve --config $configPath"
Write-Host "Log: $log"
