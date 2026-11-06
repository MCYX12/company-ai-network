<# Read-only before/after evidence for reboot, sleep and Wi-Fi recovery.
   Run before the owner saves work and performs the physical action, then afterward.
#>
param(
 [Parameter(Mandatory=$true)][string]$InstallDir,
 [Parameter(Mandatory=$true)][string]$PythonPath,
 [Parameter(Mandatory=$true)][string]$Output,
 [ValidateSet('before','after')][string]$Phase='before',
 [string]$Before,
 [ValidateSet('reboot','sleep','wifi')][string]$Scenario='reboot'
)
$ErrorActionPreference='Stop'
if(Test-Path $Output){throw 'Existing evidence preserved; choose a new output filename'}
$config=Get-Content (Join-Path $InstallDir 'client.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if($config.identity -notmatch '^[a-z][a-z0-9-]{0,63}$'){throw 'Invalid identity'}
$task=Get-ScheduledTask -TaskName ('CompanyAgent-'+$config.identity)
$boot=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
$result=[ordered]@{schema_version=1;scenario=$Scenario;phase=$Phase;observed_at=[DateTime]::UtcNow.ToString('o');boot_at=$boot;task_state=[string]$task.State;task_logon=[string]$task.Principal.LogonType;task_run_level=[string]$task.Principal.RunLevel;recovery_passed=$false;physical_action_proven=$false}
$doctor=& $PythonPath (Join-Path $InstallDir 'worker_doctor.py')
$doctorExit=$LASTEXITCODE
$result.doctor_ok=($doctorExit -in @(0,2))
$result.doctor_warning=($doctorExit -eq 2)
# The doctor contains only bounded integrity/status metadata; keep details locally.
$result.doctor=$doctor | ConvertFrom-Json
if($Phase -eq 'after'){
 if(-not $Before){throw 'After evidence requires the before snapshot'}
 $previous=Get-Content $Before -Raw -Encoding UTF8 | ConvertFrom-Json
 if($previous.scenario -ne $Scenario -or $previous.phase -ne 'before'){throw 'Before evidence does not match this scenario'}
 if($Scenario -eq 'reboot'){$result.physical_action_proven=($boot -ne $previous.boot_at)}
 else{
  # OS events are supporting evidence; presence alone is not full network/model recovery.
  $log=if($Scenario -eq 'sleep'){'System'}else{'Microsoft-Windows-WLAN-AutoConfig/Operational'}
  $events=@(Get-WinEvent -FilterHashtable @{LogName=$log;StartTime=[DateTime]::Parse($previous.observed_at)} -ErrorAction SilentlyContinue)
  $entered=@($events | Where-Object {if($Scenario -eq 'sleep'){$_.ProviderName -eq 'Microsoft-Windows-Kernel-Power' -and $_.Id -in @(42,506)}else{$_.Id -eq 8003}})
  $returned=@($events | Where-Object {if($Scenario -eq 'sleep'){($_.ProviderName -eq 'Microsoft-Windows-Power-Troubleshooter' -and $_.Id -eq 1) -or ($_.ProviderName -eq 'Microsoft-Windows-Kernel-Power' -and $_.Id -eq 507)}else{$_.Id -eq 8001}})
  # A lone connect/wake event is insufficient: require both ends in chronological order.
  $result.physical_action_proven=(@($entered | Where-Object {$start=$_.TimeCreated;@($returned | Where-Object {$_.TimeCreated -gt $start}).Count -gt 0}).Count -gt 0)
 }
 $result.recovery_passed=($result.physical_action_proven -and $result.doctor_ok -and $task.State -eq 'Running')
 $result.limits='Local worker recovery only; run a new authorized cross-device status query and fixed model self-test to complete end-to-end acceptance.'
}
[IO.File]::WriteAllText([IO.Path]::GetFullPath($Output),($result|ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
$result | ConvertTo-Json -Depth 10
if($Phase -eq 'after' -and -not $result.recovery_passed){exit 1}
if(-not $result.doctor_ok){exit 1}
