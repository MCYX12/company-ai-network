# Company Agent - proxy-isolated local forwarder for Windows (skeleton; not yet run on real Windows).
# Listens on 127.0.0.1 (never captured by a proxy TUN) and egresses through a pinned interface.
param(
  [string]$Server = "",
  [string]$InstallDir = "$env:ProgramData\CompanyAgent",
  [string]$Python = "python",
  [string]$ListenPort = "14223"
)
$ErrorActionPreference = "Stop"
if ($Server -eq "") { Write-Host "FAIL provide -Server <server ip>"; exit 1 }
$rt = Join-Path $InstallDir "runtime"
New-Item -ItemType Directory -Force -Path $rt | Out-Null
Set-Content -Path (Join-Path $rt "endpoint.txt") -Value ("tls://127.0.0.1:" + $ListenPort)
$action = New-ScheduledTaskAction -Execute $Python -Argument ("-m scripts.local_forwarder --host " + $Server + " --port 14222 --listen-port " + $ListenPort) -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName "CompanyAgentForwarder" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
Write-Host "Forwarder registered. endpoint.txt now points at tls://127.0.0.1:$ListenPort"
