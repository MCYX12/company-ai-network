<# Offline Windows caller/worker installation; no WSL or public dependency downloads. #>
param(
  [string]$InstallDir = "$env:LOCALAPPDATA\CompanyAgentClient",
  [Parameter(Mandatory=$true)][string]$PackageZip,
  [Parameter(Mandatory=$true)][string]$EnrollmentZip,
  [Parameter(Mandatory=$true)][string]$Signature,
  [Parameter(Mandatory=$true)][string]$TrustedKey,
  [string]$PythonPath = '',
  [string]$WorkerConfig = '',
  [string]$WorkerScript = '',
  [string]$Identity = '',
  [ValidateSet('UserLogon','Unattended')][string]$StartupMode='UserLogon',
  [System.Management.Automation.PSCredential]$WorkerCredential
)
$ErrorActionPreference = 'Stop'
$InstallDir=[IO.Path]::GetFullPath($InstallDir)
function Fail([string]$message) { throw $message }
if (Test-Path -LiteralPath $InstallDir) { Fail "Existing installation preserved: $InstallDir" }
if (-not $PythonPath) {
  $found=Get-Command python -ErrorAction SilentlyContinue
  if (-not $found -or $found.Source -like '*WindowsApps*') { Fail 'Provide -PythonPath for Python 3.11+.' }
  $PythonPath=$found.Source
}
$env:PYTHONUTF8='1'
$version=& $PythonPath -c 'import sys;print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor))'
if ($LASTEXITCODE -ne 0 -or [version]$version -lt [version]'3.11') { Fail 'Python 3.11+ is required.' }
if ($WorkerConfig -and -not (Test-Path -LiteralPath $WorkerConfig)) { Fail 'Worker configuration is missing.' }
if ($StartupMode -eq 'Unattended' -and (-not $WorkerConfig -or -not $WorkerCredential)) { Fail 'Unattended requires worker configuration and an explicit task credential.' }
if ($WorkerScript) {
  if (-not (Test-Path -LiteralPath $WorkerScript -PathType Leaf)) { Fail 'Worker script is missing.' }
  $workerFileName=[IO.Path]::GetFileName($WorkerScript)
  if ($workerFileName -in @('client.py','worker.py') -or [IO.Path]::GetExtension($workerFileName) -ne '.py') { Fail 'Worker script must be a distinct Python adapter file.' }
}
$taskName=''
$stage=Join-Path $env:TEMP ('CompanyAgentInstall-'+[Guid]::NewGuid().ToString('N'))
try {
  New-Item -ItemType Directory -Path $stage | Out-Null
  $stagedPackage=Join-Path $stage 'client.zip'
  Copy-Item -LiteralPath $PackageZip -Destination $stagedPackage
  $verifier=Join-Path $PSScriptRoot 'package_trust.py'
  if(-not (Test-Path -LiteralPath $verifier)){ $verifier=Join-Path $PSScriptRoot '..\packaging\client\package_trust.py' }
  & $PythonPath $verifier --package $stagedPackage --signature $Signature --trusted-key $TrustedKey
  if($LASTEXITCODE -ne 0){Fail 'Publisher verification failed before extraction.'}
  Expand-Archive -LiteralPath $stagedPackage -DestinationPath $stage
  $source=Join-Path $stage 'company-agent-client'
  & $PythonPath (Join-Path $source 'install.py') --enrollment $EnrollmentZip --destination $InstallDir --skills none --package $stagedPackage --signature $Signature --trusted-key $TrustedKey
  if ($LASTEXITCODE -ne 0) { Fail 'Offline client installation failed.' }
  $client=Get-Content -LiteralPath (Join-Path $InstallDir 'client.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($Identity -and $Identity -ne $client.identity) { Fail 'Enrollment identity does not match -Identity.' }
  $Identity=$client.identity
  Copy-Item -LiteralPath $TrustedKey -Destination (Join-Path $InstallDir 'publisher-trust.json')
  # Only this user, SYSTEM and administrators may read the individual private key.
  $certs=Join-Path $InstallDir '.runtime\certs'
  $userSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  # Elevated installation may give OWNER RIGHTS to Administrators rather than the
  # interactive user. The limited logon task must be able to read and write its root.
  & icacls.exe $InstallDir /inheritance:r /grant:r "*$($userSid):(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail 'Unable to grant the installing user access.' }
  & icacls.exe (Join-Path $InstallDir '*') /reset /T /Q | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail 'Unable to inherit installation permissions.' }
  & icacls.exe $certs /inheritance:r /grant:r "*$($userSid):(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail 'Unable to restrict enrollment credential permissions.' }
  & icacls.exe (Join-Path $certs '*') /reset /T /Q | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail 'Unable to inherit restricted credential permissions.' }
  Copy-Item -LiteralPath (Join-Path $source 'selfcheck.ps1') -Destination $InstallDir
  if ($WorkerConfig) {
    $taskName='CompanyAgent-'+$Identity
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { Fail "Existing scheduled task preserved: $taskName" }
    Copy-Item -LiteralPath $WorkerConfig -Destination (Join-Path $InstallDir 'worker.json')
    if ($WorkerScript) { Copy-Item -LiteralPath $WorkerScript -Destination (Join-Path $InstallDir $workerFileName) }
    $action=New-ScheduledTaskAction -Execute $PythonPath -Argument '-u run-worker.py' -WorkingDirectory $InstallDir
    $user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
    if($StartupMode -eq 'Unattended'){
      $credentialSid=(New-Object Security.Principal.NTAccount($WorkerCredential.UserName)).Translate([Security.Principal.SecurityIdentifier]).Value
      if($credentialSid -ne $userSid){Fail 'Task credential must be the installing owner.'}
      $user=$WorkerCredential.UserName
      $trigger=New-ScheduledTaskTrigger -AtStartup
      $principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Password -RunLevel Limited
    }else{
      $trigger=New-ScheduledTaskTrigger -AtLogOn -User $user
      $principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    }
    $settings=New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    if($StartupMode -eq 'Unattended'){
      $definition=New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings
      Register-ScheduledTask -TaskName $taskName -InputObject $definition -User $user -Password $WorkerCredential.GetNetworkCredential().Password | Out-Null
    }else{Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null}
    Start-ScheduledTask -TaskName $taskName
    $startDeadline=(Get-Date).AddSeconds(10)
    do {
      $taskState=(Get-ScheduledTask -TaskName $taskName).State
      if ($taskState -eq 'Running') { break }
      Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $startDeadline)
    if ($taskState -ne 'Running') { Fail ('Worker scheduled task failed to start: '+(Get-ScheduledTaskInfo -TaskName $taskName).LastTaskResult) }
  }
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir 'selfcheck.ps1') -InstallDir $InstallDir -PythonPath $PythonPath
  $checkExit=$LASTEXITCODE
  if ($checkExit -eq 1) { Fail 'Installed client self-check failed; installation retained for diagnosis.' }
  if ($checkExit -notin @(0,2)) { Fail "Unexpected self-check exit code: $checkExit" }
  [ordered]@{installed=$InstallDir;identity=$Identity;selfcheck_exit=$checkExit;scheduled_task=$taskName;startup=$StartupMode;publisher_verified=$true} | ConvertTo-Json
} finally {
  if(Test-Path -LiteralPath $stage){Remove-Item -LiteralPath $stage -Recurse -Force}
}
