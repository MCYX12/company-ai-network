<# Administrator operation. Preview by default; use approved NTP peers.
   The persistent SYSTEM task executes only Windows' protected w32tm.exe.
   Task exit 0 alone is insufficient: verify the actual source and sync status.
#>
param(
 [Parameter(Mandatory=$true)][string[]]$Peers,
 [switch]$Apply
)
$ErrorActionPreference='Stop'
if($Peers.Count -lt 1 -or $Peers.Count -gt 4){throw 'Provide one to four approved NTP peers'}
foreach($peer in $Peers){if($peer.Length -gt 253 -or $peer -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$'){throw 'Invalid NTP peer; flags are added by this script'}}
if(@($Peers | ForEach-Object {$_.ToLowerInvariant()} | Select-Object -Unique).Count -ne $Peers.Count){throw 'Duplicate NTP peers'}
$name='CompanyAgent-TimeSynchronization'
$system=[Environment]::GetFolderPath('System')
$exe=Join-Path $system 'w32tm.exe'
$reg=Join-Path $system 'reg.exe'
$sc=Join-Path $system 'sc.exe'
$task=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
$oldService=Get-Service W32Time
$oldStartType=$oldService.StartType
$oldStatus=$oldService.Status
$oldDelayed=(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time' -Name DelayedAutoStart -ErrorAction SilentlyContinue).DelayedAutoStart
$joined=(Get-CimInstance Win32_ComputerSystem).PartOfDomain
$policy=Test-Path 'HKLM:\SOFTWARE\Policies\Microsoft\W32Time'
$peerList=($Peers | ForEach-Object {$_+',0x8'}) -join ' '
if(-not $Apply){
 @{apply=$false;peers=$peerList;domain_joined=$joined;time_policy_present=$policy;task_exists=($null -ne $task);requires_administrator=$true;source=(& $exe /query /source | Out-String).Trim()} | ConvertTo-Json
 exit 0
}
$principal=[Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Administrator token required'}
if($joined -or $policy){throw 'Domain or policy-managed time configuration preserved; use the enterprise time administrator'}
if($task -and (@($task.Actions).Count -ne 1 -or $task.Actions[0].Execute -ne $exe -or $task.Actions[0].Arguments -ne '/resync /rediscover' -or $task.Principal.UserId -notin @('SYSTEM','S-1-5-18','NT AUTHORITY\SYSTEM'))){throw 'Existing task is not the managed time task; preserved'}
$base=Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'CompanyAgentOperations-Time'
if(Test-Path $base){if((Get-Item -LiteralPath $base).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Unsafe backup directory'}}
else{New-Item -ItemType Directory -Path $base | Out-Null}
& (Join-Path $system 'icacls.exe') $base /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if($LASTEXITCODE){throw 'Backup permissions could not be restricted'}
$backup=Join-Path $base ('before-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ'))
New-Item -ItemType Directory -Path $backup | Out-Null
& $reg export 'HKLM\SYSTEM\CurrentControlSet\Services\W32Time' (Join-Path $backup 'W32Time.reg') /y | Out-Null
if($LASTEXITCODE){throw 'Time configuration backup failed'}
$xml=if($task){Export-ScheduledTask -TaskName $name}else{$null}
if($xml){[IO.File]::WriteAllText((Join-Path $backup 'task.xml'),$xml,[Text.UTF8Encoding]::new($false))}
# Keep the native trigger buffer alive for exact SCM rollback, including custom triggers.
if(-not ('CompanyAgentTimeTriggerSnapshot' -as [type])){
 Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public sealed class CompanyAgentTimeTriggerSnapshot : IDisposable {
 [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr OpenSCManager(string m,string d,uint a);
 [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr OpenService(IntPtr m,string n,uint a);
 [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool QueryServiceConfig2(IntPtr s,uint l,IntPtr b,uint z,out uint n);
 [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool ChangeServiceConfig2(IntPtr s,uint l,IntPtr b);
 [DllImport("advapi32.dll")] static extern bool CloseServiceHandle(IntPtr h);
 IntPtr manager,service,buffer;
 public int TriggerCount { get { return Marshal.ReadInt32(buffer); } }
 public CompanyAgentTimeTriggerSnapshot() {
  try {
   manager=OpenSCManager(null,null,1); if(manager==IntPtr.Zero) throw new Win32Exception();
   service=OpenService(manager,"W32Time",3); if(service==IntPtr.Zero) throw new Win32Exception();
   uint size; QueryServiceConfig2(service,8,IntPtr.Zero,0,out size);
   if(size==0) throw new Win32Exception();
   buffer=Marshal.AllocHGlobal((int)size);
   if(!QueryServiceConfig2(service,8,buffer,size,out size)) throw new Win32Exception();
  } catch { Dispose(); throw; }
 }
 public void Restore() {
  // Windows rejects clearing triggers when there are already none.
  if(Marshal.ReadInt32(buffer)==0) {
   using(var current=new CompanyAgentTimeTriggerSnapshot()) {
    if(Marshal.ReadInt32(current.buffer)==0) return;
   }
  }
  if(!ChangeServiceConfig2(service,8,buffer)) throw new Win32Exception();
 }
 public void Dispose() { if(buffer!=IntPtr.Zero){Marshal.FreeHGlobal(buffer);buffer=IntPtr.Zero;} if(service!=IntPtr.Zero){CloseServiceHandle(service);service=IntPtr.Zero;} if(manager!=IntPtr.Zero){CloseServiceHandle(manager);manager=IntPtr.Zero;} }
}
'@
}
$triggers=[CompanyAgentTimeTriggerSnapshot]::new()
try{
 # Non-domain machines need ordinary automatic startup, without trigger-start suppression.
 if($triggers.TriggerCount -gt 0){
  & $sc triggerinfo W32Time delete | Out-Null
  if($LASTEXITCODE){throw 'Time service trigger configuration failed'}
 }
 & $sc config W32Time 'start=' auto | Out-Null
 if($LASTEXITCODE){throw 'Time service automatic startup configuration failed'}
 & $exe /config ('/manualpeerlist:'+$peerList) /syncfromflags:manual /update | Out-Null
 if($LASTEXITCODE){throw 'NTP configuration failed'}
 Set-Service W32Time -StartupType Automatic
 $boot=New-ScheduledTaskTrigger -AtStartup;$boot.Delay='PT30S'
 $eventClass=Get-CimClass -Namespace 'Root/Microsoft/Windows/TaskScheduler' -ClassName MSFT_TaskEventTrigger
 $resume=New-CimInstance -CimClass $eventClass -ClientOnly -Property @{Enabled=$true;Delay='PT10S';Subscription='<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[(Provider[@Name="Microsoft-Windows-Power-Troubleshooter"] and EventID=1) or (Provider[@Name="Microsoft-Windows-Kernel-Power"] and EventID=507)]]</Select></Query></QueryList>'}
 $periodic=New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(20) -RepetitionInterval (New-TimeSpan -Minutes 5)
 $action=New-ScheduledTaskAction -Execute $exe -Argument '/resync /rediscover'
 $owner=New-ScheduledTaskPrincipal -UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest
 $settings=New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
 Register-ScheduledTask -TaskName $name -InputObject (New-ScheduledTask -Action $action -Trigger @($boot,$periodic,$resume) -Principal $owner -Settings $settings) -Force | Out-Null
 $before=[DateTime]::UtcNow
 Restart-Service W32Time
 $wait=[Diagnostics.Stopwatch]::StartNew()
 do{
 Start-Sleep -Seconds 3
 $actual=Get-ScheduledTask -TaskName $name;$info=Get-ScheduledTaskInfo -TaskName $name
 $source=(& $exe /query /source | Out-String).Trim()
 $status=(& $exe /query /status /verbose | Out-String)
 # Leading numeric fields are Leap Indicator and Stratum in localized w32tm output.
 $numbers=@($status -split "`r?`n" | ForEach-Object {if($_ -match '^\s*[^:]+:\s*(\d+)'){[int]$Matches[1]}})
 $sourceVerified=@($Peers | Where-Object {$source -match ('^'+[regex]::Escape($_)+',0x8\s*$')}).Count -eq 1
 $ok=($actual.State -ne 'Running' -and $actual.Actions[0].Execute -eq $exe -and $actual.Actions[0].Arguments -eq '/resync /rediscover' -and $info.LastRunTime.ToUniversalTime() -gt $before -and $info.LastTaskResult -eq 0 -and $sourceVerified -and $numbers.Count -ge 2 -and $numbers[0] -eq 0 -and $numbers[1] -ge 1 -and $numbers[1] -le 15)
 }while(-not $ok -and $wait.Elapsed.TotalSeconds -lt 105)
 $wait.Stop()
 @{applied=$true;verified=$ok;source=$source;status=$status;task=$name;task_last_run=$info.LastRunTime.ToUniversalTime().ToString('o');task_result=$info.LastTaskResult;backup=$backup;manual_set_date_used=$false;credentials_saved=$false;physical_recovery_tested=$false} | ConvertTo-Json
 if(-not $ok){throw 'Automatic synchronization not verified'}
}catch{
 $failure=$_
 if($xml){Register-ScheduledTask -TaskName $name -Xml $xml -Force | Out-Null}else{Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue}
 & $reg import (Join-Path $backup 'W32Time.reg') | Out-Null
 if($LASTEXITCODE){throw 'Time repair failed; registry rollback also failed; inspect the protected backup'}
 $oldMode=if($oldStartType -eq 'Automatic'){if($oldDelayed -eq 1){'delayed-auto'}else{'auto'}}elseif($oldStartType -eq 'Manual'){'demand'}else{'disabled'}
 & $sc config W32Time 'start=' $oldMode | Out-Null
 if($LASTEXITCODE){throw 'Time repair failed; startup mode rollback failed; inspect the protected backup'}
 $triggers.Restore()
 if($oldStatus -eq 'Running'){Restart-Service W32Time}else{Stop-Service W32Time -ErrorAction SilentlyContinue}
 throw $failure
}finally{$triggers.Dispose()}
