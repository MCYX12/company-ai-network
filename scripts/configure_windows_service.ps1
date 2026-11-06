<# Opt-in SCM backend. Preview is default. Drain/stop and disable the scheduler first.
   Use a dedicated non-administrator owner with SeServiceLogonRight; no policy elevation.
#>
param(
 [Parameter(Mandatory=$true)][string]$InstallDir,
 [Parameter(Mandatory=$true)][string]$PythonPath,
 [System.Management.Automation.PSCredential]$Credential,
 [switch]$Apply,
 [switch]$Remove
)
$ErrorActionPreference='Stop'
$InstallDir=[IO.Path]::GetFullPath($InstallDir)
$config=Get-Content (Join-Path $InstallDir 'client.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if($config.identity -notmatch '^[a-z][a-z0-9-]{0,63}$'){throw 'Invalid identity'}
$name='CompanyAgent-'+$config.identity
$exe=Join-Path $InstallDir 'CompanyAgentService.exe'
$existing=Get-Service -Name $name -ErrorAction SilentlyContinue
$task=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if(-not $Apply){@{service=$name;exists=($null -ne $existing);apply=$false;remove=[bool]$Remove;requires_standard_owner=$true;requires_service_logon_right=$true;task_must_be_disabled=$true}|ConvertTo-Json;exit 0}
if($task -and ($task.State -eq 'Running' -or $task.Settings.Enabled)){throw 'Drain, stop and disable the managed scheduled task first.'}
if($existing){
 $record=Get-CimInstance Win32_Service -Filter "Name='$name'"
 if(-not $record.PathName.StartsWith(('"'+$exe+'" '),[StringComparison]::OrdinalIgnoreCase)){throw 'Unmanaged service preserved'}
 if($existing.Status -ne 'Stopped'){throw 'Drain and stop the service before changing it'}
 if(-not $Remove){throw 'Existing managed service preserved; remove explicitly before reconfiguration'}
 & sc.exe delete $name | Out-Null
 if($LASTEXITCODE){throw 'Service removal failed'}
 @{service=$name;removed=$true;credentials_and_runtime_preserved=$true}|ConvertTo-Json;exit 0
}
if($Remove){throw 'No managed service to remove'}
if(-not $Credential){throw 'Explicit standard owner credential is required'}
$ownerSid=(New-Object Security.Principal.NTAccount($Credential.UserName)).Translate([Security.Principal.SecurityIdentifier]).Value
$current=[Security.Principal.WindowsIdentity]::GetCurrent()
if($ownerSid -ne $current.User.Value){throw 'Run as the installation owner (elevated by enterprise policy when necessary)'}
if(@($current.Groups | Where-Object {$_.Value -eq 'S-1-5-32-544'}).Count){throw 'SCM owner must be a non-administrator; retain Limited scheduled task for administrator-owned installations'}
& $PythonPath (Join-Path $InstallDir 'worker_doctor.py')
if($LASTEXITCODE -notin @(0,2)){throw 'Worker self-check failed'}
$source=Join-Path $InstallDir 'WorkerService.cs'
$compiler=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if(Test-Path $exe){throw 'Existing service binary preserved'}
& $compiler /nologo /target:exe /reference:System.ServiceProcess.dll "/out:$exe" $source
if($LASTEXITCODE){throw 'SCM host compilation failed'}
$created=$false
try {
 $command='"'+$exe+'" "'+$name+'" "'+[IO.Path]::GetFullPath($PythonPath)+'" "'+$InstallDir+'"'
 New-Service -Name $name -BinaryPathName $command -Credential $Credential -StartupType Automatic -Description 'Company Agent worker (owner account, no desktop restart)' | Out-Null
 $created=$true
 & sc.exe failure $name reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
 if($LASTEXITCODE){throw 'SCM recovery configuration failed'}
 & sc.exe failureflag $name 1 | Out-Null
 if($LASTEXITCODE){throw 'SCM non-crash recovery configuration failed'}
 Start-Service $name
 $actual=Get-CimInstance Win32_Service -Filter "Name='$name'"
 if($actual.State -ne 'Running' -or $actual.StartMode -ne 'Auto'){throw 'SCM read-back failed'}
 @{service=$name;state=$actual.State;startup=$actual.StartMode;owner=$actual.StartName;reboot_tested=$false}|ConvertTo-Json
} catch {
 if($created){Stop-Service $name -Force -ErrorAction SilentlyContinue;& sc.exe delete $name | Out-Null}
 throw
}
