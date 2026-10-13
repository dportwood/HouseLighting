# Creates "House Lights" shortcuts (Desktop and Start menu) that open the player control window:
# start / stop / restart / status / log.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1 -Uninstall

param(
    [string]$Config = "config.toml",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$name = "House Lights.lnk"
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) $name),
    (Join-Path ([Environment]::GetFolderPath("Programs")) $name)
)

if ($Uninstall) {
    $targets | Where-Object { Test-Path $_ } | ForEach-Object { Remove-Item $_; Write-Host "Removed $_" }
    return
}

$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source
$configPath = Join-Path $repo $Config
$shell = New-Object -ComObject WScript.Shell
foreach ($path in $targets) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "-m houselights control --config `"$configPath`""
    $lnk.WorkingDirectory = $repo
    $lnk.Description = "Start, stop and check the house light show player"
    $lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,43"
    $lnk.Save()
    Write-Host "Created $path"
}
