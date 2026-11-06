<# Exit codes: 0 normal, 1 failed, 2 warning. No model call. #>
param([string]$InstallDir="$env:LOCALAPPDATA\CompanyAgentClient", [string]$Identity='', [string]$PythonPath='')
$ErrorActionPreference='Stop'
$stderrPath=Join-Path $env:TEMP ('CompanyAgentDoctor-'+[Guid]::NewGuid().ToString('N')+'.txt')
try {
  if (-not $PythonPath) {
    $found=Get-Command python -ErrorAction SilentlyContinue
    if (-not $found -or $found.Source -like '*WindowsApps*') { throw 'Python not found; provide -PythonPath.' }
    $PythonPath=$found.Source
  }
  $client=Join-Path $InstallDir 'client.py'
  if (-not (Test-Path -LiteralPath $client)) { throw 'Installed client.py not found.' }
  $env:PYTHONUTF8='1'
  $previousErrorPreference=$ErrorActionPreference
  $ErrorActionPreference='Continue'
  $output=& $PythonPath $client --doctor 2> $stderrPath
  $nativeExit=$LASTEXITCODE
  $ErrorActionPreference=$previousErrorPreference
  if ($nativeExit -ne 0) { $details=Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue; throw "Client diagnostic exited $nativeExit : $details" }
  $text=($output -join "`n")
  $result=$text | ConvertFrom-Json
  if ($result.code -ne 'DIAGNOSTIC' -or -not $result.checks) { throw 'Invalid diagnostic response.' }
  if (@($result.checks | Where-Object { $_.status -notin @('ok','warning','error') }).Count) { throw 'Invalid diagnostic check status.' }
  Write-Output $text
  $workerConfig=Join-Path $InstallDir 'worker.json'
  $workerDoctor=Join-Path $InstallDir 'worker_doctor.py'
  $workerWarning=$false
  if ((Test-Path -LiteralPath $workerConfig) -and (Test-Path -LiteralPath $workerDoctor)) {
    & $PythonPath $workerDoctor
    if ($LASTEXITCODE -eq 2) { $workerWarning=$true }
    elseif ($LASTEXITCODE -ne 0) { exit 1 }
  }
  if(@($result.checks | Where-Object status -eq 'error').Count){exit 1}
  if($workerWarning -or @($result.checks | Where-Object status -eq 'warning').Count){exit 2}
  exit 0
} catch { Write-Output ('FAIL '+$_.Exception.Message); exit 1 } finally {
  if(Test-Path -LiteralPath $stderrPath){Remove-Item -LiteralPath $stderrPath -Force}
}
