<# Owner operation; preview by default. Uses an Interactive/Limited AtLogon task.
   Does not log out, close the desktop, or change the original Harness profile.
   A manual trigger verifies readiness only, not an actual new-login trigger.
#>
param(
 [Parameter(Mandatory=$true)][string]$InstallDir,
 [Parameter(Mandatory=$true)][string]$PythonPath,
 [string]$TaskName=('CompanyAgent-DeepSeekDesktop-'+$env:USERNAME),
 [switch]$Apply
)
$ErrorActionPreference='Stop'
if($TaskName -notmatch '^CompanyAgent-DeepSeekDesktop-[A-Za-z0-9._-]{1,64}$'){throw 'Invalid managed desktop task name'}
$InstallDir=[IO.Path]::GetFullPath($InstallDir)
$PythonPath=[IO.Path]::GetFullPath($PythonPath)
$cfg=Get-Content -LiteralPath (Join-Path $InstallDir 'worker.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if($cfg.native_work.enabled -ne $true){throw 'Enable the owner-approved native_work integration before configuring desktop startup'}
if(-not [IO.Path]::IsPathRooted($cfg.native_work.app) -or -not [IO.Path]::IsPathRooted($cfg.native_work.home)){throw 'Absolute original desktop paths required'}
$exe=Join-Path $cfg.native_work.app 'DeepSeek Harness.exe'
$owner=[Security.Principal.WindowsIdentity]::GetCurrent()
$client=Get-Content -LiteralPath (Join-Path $InstallDir 'client.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if($client.identity -notmatch '^[a-z][a-z0-9-]{0,63}$'){throw 'Invalid installed identity'}
$workerTask=Get-ScheduledTask -TaskName ('CompanyAgent-'+$client.identity)
$workerSid=(New-Object Security.Principal.NTAccount($workerTask.Principal.UserId)).Translate([Security.Principal.SecurityIdentifier]).Value
if($workerSid -ne $owner.User.Value){throw 'Run as the installed worker owner'}
$dir=Join-Path $env:LOCALAPPDATA 'CompanyAgentDesktopIntegration'
$path=Join-Path $dir 'start-original-desktop.ps1'
$powershell=Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
$task=Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if($task){
 $sid=(New-Object Security.Principal.NTAccount($task.Principal.UserId)).Translate([Security.Principal.SecurityIdentifier]).Value
 if($sid -ne $owner.User.Value -or [string]$task.Principal.LogonType -ne 'Interactive' -or [string]$task.Principal.RunLevel -ne 'Limited' -or @($task.Actions).Count -ne 1 -or $task.Actions[0].Execute -ne $powershell -or $task.Actions[0].WorkingDirectory -ne $dir -or $task.Actions[0].Arguments -ne ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+$path+'"')){throw 'Existing task is not the managed owner desktop task; preserved'}
}
if(-not $Apply){
 @{apply=$false;task=$TaskName;task_exists=($null -ne $task);owner=$owner.Name;desktop=$exe;logon_type='Interactive';run_level='Limited';retries=3;checks_native_api=$true;actual_new_login_tested=$false} | ConvertTo-Json
 exit 0
}
if(-not (Test-Path -LiteralPath $PythonPath -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $InstallDir 'worker_doctor.py') -PathType Leaf)){throw 'Installed Python and worker doctor required'}
if((Get-AuthenticodeSignature -FilePath $exe).Status -ne 'Valid'){throw 'Original desktop signature invalid'}
if($task -and $task.State -eq 'Running'){throw 'Existing startup attempt is running'}
if(Test-Path $dir){if((Get-Item -LiteralPath $dir).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Unsafe integration directory'}}else{New-Item -ItemType Directory -Path $dir | Out-Null}
if((Test-Path $path) -and -not $task){throw 'Unmanaged existing launcher preserved'}
$backup=Join-Path $dir ('before-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ'))
New-Item -ItemType Directory -Path $backup | Out-Null
$hadFile=Test-Path $path
if($hadFile){Copy-Item -LiteralPath $path -Destination (Join-Path $backup 'start-original-desktop.ps1')}
$xml=if($task){Export-ScheduledTask -TaskName $TaskName}else{$null}
if($xml){[IO.File]::WriteAllText((Join-Path $backup 'task.xml'),$xml,[Text.UTF8Encoding]::new($false))}
$script=@'
$ErrorActionPreference='Stop'
$exe='__EXE__'
$root='__ROOT__'
$python='__PYTHON__'
$dir='__DIR__'
$session=(Get-Process -Id $PID).SessionId
$record=[ordered]@{schema_version=1;run_id=[Guid]::NewGuid().ToString();started_at=[DateTime]::UtcNow.ToString('o');completed_at=$null;session_id=$session;owner_sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;gui_launched=$false;existing_gui=$false;native_api_verified=$false;success=$false;hint=$null;desktop_history_read=$false}
function Save-Result {
 $record.completed_at=[DateTime]::UtcNow.ToString('o')
 $temp=Join-Path $dir ('startup-result-'+[Guid]::NewGuid().ToString('N')+'.tmp')
 [IO.File]::WriteAllText($temp,($record | ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
 $target=Join-Path $dir 'startup-result.json'
 # PowerShell coerces ordinary $null to an empty string for this .NET parameter.
 if(Test-Path -LiteralPath $target){[IO.File]::Replace($temp,$target,[NullString]::Value)}else{[IO.File]::Move($temp,$target)}
}
try {
 if($session -le 0){throw 'INTERACTIVE_SESSION_REQUIRED'}
 if((Get-AuthenticodeSignature -FilePath $exe).Status -ne 'Valid'){throw 'DESKTOP_SIGNATURE_INVALID'}
 $cfg=Get-Content -LiteralPath "$root\worker.json" -Raw -Encoding UTF8 | ConvertFrom-Json
 if($cfg.native_work.enabled -ne $true -or $cfg.native_work.app -ne (Split-Path $exe) -or $cfg.native_work.home -ne '__HOME__'){throw 'DESKTOP_INTEGRATION_CONFIG_MISMATCH'}
 $gui=@(Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -eq $exe -and $_.SessionId -eq $session -and $_.CommandLine -notmatch '--type=|--expose-internals|--run-as-node'})
 $record.existing_gui=($gui.Count -gt 0)
 if(-not $record.existing_gui){
  $env:ELECTRON_RUN_AS_NODE=$null
  Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe)
  $record.gui_launched=$true
 }
 $ready=$false
 for($attempt=0;$attempt -lt 8;$attempt++){
  $env:PYTHONUTF8='1'
  $raw=& $python "$root\worker_doctor.py" 2>$null
  $exit=$LASTEXITCODE
  try {$doctor=$raw | ConvertFrom-Json; $native=@($doctor.checks | Where-Object stage -eq 'native_work');$ready=($exit -in @(0,2) -and $doctor.code -eq 'DIAGNOSTIC' -and $native.Count -eq 1 -and $native[0].status -eq 'ok')}catch{$ready=$false}
  if($ready){break}
  Start-Sleep -Seconds 5
 }
 if(-not $ready){throw 'DESKTOP_API_NOT_READY'}
 $record.native_api_verified=$true;$record.success=$true
 Save-Result
 exit 0
}catch{
 $allowed=@('INTERACTIVE_SESSION_REQUIRED','DESKTOP_SIGNATURE_INVALID','DESKTOP_INTEGRATION_CONFIG_MISMATCH','DESKTOP_API_NOT_READY')
 $record.hint=if($_.Exception.Message -in $allowed){$_.Exception.Message}else{'DESKTOP_STARTUP_FAILED'}
 Save-Result
 exit 1
}

'@
foreach($item in @(@('__EXE__',$exe),@('__ROOT__',$InstallDir),@('__PYTHON__',$PythonPath),@('__DIR__',$dir),@('__HOME__',[string]$cfg.native_work.home))){$script=$script.Replace($item[0],$item[1].Replace("'","''"))}
try{
 # Windows PowerShell 5.1 needs a BOM to recognize generated non-ASCII paths.
 [IO.File]::WriteAllText($path,$script,[Text.UTF8Encoding]::new($true))
 $tokens=$null;$errors=$null
 [Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors) | Out-Null
 if($errors.Count){throw 'Launcher parse failed'}
 $trigger=New-ScheduledTaskTrigger -AtLogOn -User $owner.Name;$trigger.Delay='PT10S'
 $principal=New-ScheduledTaskPrincipal -UserId $owner.Name -LogonType Interactive -RunLevel Limited
 $action=New-ScheduledTaskAction -Execute $powershell -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+$path+'"') -WorkingDirectory $dir
 $settings=New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 3) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
 Register-ScheduledTask -TaskName $TaskName -InputObject (New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings) -Force | Out-Null
 $oldRun=$null
 if(Test-Path (Join-Path $dir 'startup-result.json')){$oldRun=(Get-Content (Join-Path $dir 'startup-result.json') -Raw -Encoding UTF8 | ConvertFrom-Json).run_id}
 $started=[DateTime]::UtcNow
 Start-ScheduledTask -TaskName $TaskName
 $until=[DateTime]::UtcNow.AddSeconds(105)
 do{Start-Sleep -Seconds 2;$actual=Get-ScheduledTask -TaskName $TaskName}while($actual.State -eq 'Running' -and [DateTime]::UtcNow -lt $until)
 $info=Get-ScheduledTaskInfo -TaskName $TaskName
 $result=Get-Content -LiteralPath (Join-Path $dir 'startup-result.json') -Raw -Encoding UTF8 | ConvertFrom-Json
 if($actual.State -eq 'Running' -or $info.LastTaskResult -ne 0 -or $result.success -ne $true -or $result.native_api_verified -ne $true -or -not $result.run_id -or $result.run_id -eq $oldRun -or [DateTime]::Parse($result.started_at).ToUniversalTime() -lt $started.AddSeconds(-1)){throw 'Startup readiness verification failed'}
 @{applied=$true;task=$TaskName;last_result=$info.LastTaskResult;startup_result=$result;backup=$backup;actual_new_login_tested=$false;original_desktop_profile_changed=$false;credentials_saved=$false} | ConvertTo-Json -Depth 6
}catch{
 $failure=$_
 # Stop only this managed readiness attempt before restoring its old files/task.
 $attempt=Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
 if($attempt -and $attempt.State -eq 'Running'){Stop-ScheduledTask -TaskName $TaskName}
 if($xml){Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null}else{Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue}
 if($hadFile){Copy-Item -LiteralPath (Join-Path $backup 'start-original-desktop.ps1') -Destination $path -Force}else{Remove-Item -LiteralPath $path -ErrorAction SilentlyContinue}
 throw $failure
}
