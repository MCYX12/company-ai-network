<# Preview/change only the managed worker's startup mode; never restart the desktop.
   Stop/drain the worker before -Apply. Password stays in Task Scheduler's protected storage.
#>
param(
  [Parameter(Mandatory=$true)][string]$InstallDir,
  [ValidateSet('UserLogon','Unattended')][string]$StartupMode='UserLogon',
  [System.Management.Automation.PSCredential]$Credential,
  [switch]$Apply
)
$ErrorActionPreference='Stop'
$InstallDir=[IO.Path]::GetFullPath($InstallDir)
$config=Get-Content -LiteralPath (Join-Path $InstallDir 'client.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if($config.identity -notmatch '^[a-z][a-z0-9-]{0,63}$'){throw 'Invalid client identity'}
$taskName='CompanyAgent-'+$config.identity
$task=Get-ScheduledTask -TaskName $taskName
if(@($task.Actions).Count -ne 1 -or $task.Actions[0].WorkingDirectory -ne $InstallDir -or $task.Actions[0].Arguments -ne '-u run-worker.py'){
  throw 'Existing task is not the managed worker; preserved.'
}
$owner=$task.Principal.UserId
$ownerSid=(New-Object Security.Principal.NTAccount($owner)).Translate([Security.Principal.SecurityIdentifier]).Value
$currentSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if($ownerSid -ne $currentSid){throw 'Run as the installed worker owner.'}
if(-not $Apply){
  [ordered]@{task=$taskName;current_logon_type=[string]$task.Principal.LogonType;desired_startup=$StartupMode;apply=$false;requires_stopped_worker=$true;requires_credential=($StartupMode -eq 'Unattended' -or [string]$task.Principal.LogonType -eq 'Password')} | ConvertTo-Json
  exit 0
}
if($task.State -eq 'Running'){throw 'Drain and stop the worker before applying startup changes.'}
if($StartupMode -eq 'Unattended' -or [string]$task.Principal.LogonType -eq 'Password'){
  if(-not $Credential){throw 'Provide an explicit owner PSCredential for unattended startup or rollback.'}
  $credentialSid=(New-Object Security.Principal.NTAccount($Credential.UserName)).Translate([Security.Principal.SecurityIdentifier]).Value
  if($credentialSid -ne $ownerSid){throw 'Credential must belong to the installed owner.'}
}
$stamp=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
$backup=Join-Path $InstallDir ('worker-task-before-'+$stamp+'.xml')
$xml=Export-ScheduledTask -TaskName $taskName
[IO.File]::WriteAllText($backup,$xml,[Text.UTF8Encoding]::new($false))
$settings=New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
try{
  if($StartupMode -eq 'Unattended'){
    $trigger=New-ScheduledTaskTrigger -AtStartup
    $principal=New-ScheduledTaskPrincipal -UserId $owner -LogonType Password -RunLevel Limited
    $definition=New-ScheduledTask -Action $task.Actions -Trigger $trigger -Settings $settings -Principal $principal
    Register-ScheduledTask -TaskName $taskName -InputObject $definition -User $Credential.UserName -Password $Credential.GetNetworkCredential().Password -Force | Out-Null
  }else{
    $trigger=New-ScheduledTaskTrigger -AtLogOn -User $owner
    $principal=New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $task.Actions -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
  }
  $actual=Get-ScheduledTask -TaskName $taskName
  $expectedLogon=if($StartupMode -eq 'Unattended'){'Password'}else{'Interactive'}
  if([string]$actual.Principal.LogonType -ne $expectedLogon -or [string]$actual.Principal.RunLevel -ne 'Limited'){throw 'Task read-back mismatch'}
  [ordered]@{task=$taskName;startup=$StartupMode;logon_type=[string]$actual.Principal.LogonType;run_level=[string]$actual.Principal.RunLevel;backup=$backup;applied=$true;restart_required=$true} | ConvertTo-Json
}catch{
  # Interactive rollback does not need a secret. Password rollback uses the supplied owner credential.
  if([string]$task.Principal.LogonType -eq 'Password' -and $Credential){Register-ScheduledTask -TaskName $taskName -Xml $xml -User $Credential.UserName -Password $Credential.GetNetworkCredential().Password -Force | Out-Null}
  elseif([string]$task.Principal.LogonType -ne 'Password'){Register-ScheduledTask -TaskName $taskName -Xml $xml -Force | Out-Null}
  throw
}
