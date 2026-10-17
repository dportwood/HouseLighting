# Copies the projector agent to a freshly flashed Pi and runs its one-time setup over SSH.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup-projector.ps1 -PiHost projector1.local -Name "Projector 1"
#
# You'll be asked for the Pi user's password (set in Raspberry Pi Imager) for each step.

param(
    [Parameter(Mandatory)] [string]$PiHost,
    [string]$User = "pi",
    [string]$Name = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$src = Join-Path $repo "projector"
if (-not $Name) { $Name = ($PiHost -split '\.')[0] }
$target = "$User@$PiHost"

Write-Host "Copying agent to $target ..."
ssh $target "mkdir -p ~/hl-projector-setup"
scp "$src\hl_projector.py" "$src\setup.sh" "${target}:hl-projector-setup/"
if ($LASTEXITCODE -ne 0) { throw "Copy failed" }

Write-Host "Running setup (installs packages; a few minutes) ..."
ssh -t $target "sed -i 's/\r$//' ~/hl-projector-setup/setup.sh && sudo bash ~/hl-projector-setup/setup.sh '$Name'"
if ($LASTEXITCODE -ne 0) { throw "Setup failed" }

Write-Host "`nChecking the agent from this PC ..."
try {
    $s = Invoke-RestMethod "http://${PiHost}:8090/api/status" -TimeoutSec 10
    Write-Host "OK: '$($s.name)' mode=$($s.mode) free=$([math]::Round($s.disk_free/1GB,1)) GB"
} catch {
    Write-Host "Couldn't reach http://${PiHost}:8090 yet: $($_.Exception.Message)"
}
