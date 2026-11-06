param([Parameter(Mandatory=$true)][string]$PythonPath)
$ErrorActionPreference='Stop'
$stage=Join-Path $env:TEMP ('CompanyAgentScmTest-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
$python=$PythonPath
$source=Join-Path $PSScriptRoot '..\packaging\windows\WorkerService.cs'
& "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:exe /reference:System.ServiceProcess.dll "/out:$stage\CompanyAgentService.exe" $source
if($LASTEXITCODE){throw 'Service host compilation failed'}
$fixture=Join-Path $stage 'scm-fixture';New-Item -ItemType Directory -Path $fixture -Force | Out-Null
@'
import subprocess,sys,time
from pathlib import Path
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)'])
Path('child.pid').write_text(str(p.pid))
Path('started').write_text('SCM_OK')
time.sleep(300)
'@ | Set-Content (Join-Path $fixture 'run-worker.py') -Encoding UTF8
& icacls.exe $stage /grant '*S-1-5-19:(OI)(CI)RX' /T /Q | Out-Null
if($LASTEXITCODE){throw 'Fixture read permission failed'}
& icacls.exe $fixture /grant '*S-1-5-19:(OI)(CI)M' /T /Q | Out-Null
$name='CompanyAgent-QA-'+[Guid]::NewGuid().ToString('N')
$exe=Join-Path $stage 'CompanyAgentService.exe'
$command='"'+$exe+'" "'+$name+'" "'+$python+'" "'+$fixture+'"'
$created=$false
try {
 New-Service -Name $name -BinaryPathName $command -StartupType Manual -Credential (New-Object Management.Automation.PSCredential('NT AUTHORITY\LocalService',(New-Object Security.SecureString))) | Out-Null
 $created=$true;Start-Service $name
 $deadline=(Get-Date).AddSeconds(10);while(-not (Test-Path (Join-Path $fixture 'started')) -and (Get-Date) -lt $deadline){Start-Sleep -Milliseconds 100}
 if(-not (Test-Path (Join-Path $fixture 'started'))){throw 'SCM fixture did not run'}
 $child=[int](Get-Content (Join-Path $fixture 'child.pid'))
 Stop-Service $name
 Start-Sleep -Seconds 1
 if(Get-Process -Id $child -ErrorAction SilentlyContinue){throw 'SCM descendant survived stop'}
 @{scm_native_start_stop=$true;descendant_stopped=$true;account='LocalService';production_worker_migrated=$false}|ConvertTo-Json
}finally{if($created){Stop-Service $name -Force -ErrorAction SilentlyContinue;& sc.exe delete $name | Out-Null}}

# Keep the bounded fixture output for failed-run diagnosis; no user data was used.
