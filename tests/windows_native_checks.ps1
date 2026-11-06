<# Run on Windows to verify native PowerShell parsing and self-check exit semantics. #>
param([Parameter(Mandatory=$true)][string]$ScriptsDir,[Parameter(Mandatory=$true)][string]$PythonPath,[Parameter(Mandatory=$true)][string]$ClientRoot)
$ErrorActionPreference='Stop'
$root=Join-Path $env:TEMP ('CompanyAgentNativeChecks-'+[Guid]::NewGuid().ToString('N'))
$rows=@()
try {
  New-Item -ItemType Directory -Path $root | Out-Null
  foreach($name in @('install_windows.ps1','selfcheck.ps1')) {
    $tokens=$null;$errors=$null
    [Management.Automation.Language.Parser]::ParseFile((Join-Path $ScriptsDir $name),[ref]$tokens,[ref]$errors) | Out-Null
    $rows += [pscustomobject]@{name=('parse_'+$name);passed=($errors.Count -eq 0);detail=($errors.Message -join '; ')}
  }
  $cases=@(
    @{name='normal';status='ok';body='';native=0;expected=0},
    @{name='warning';status='warning';body='';native=0;expected=2},
    @{name='error';status='error';body='';native=0;expected=1},
    @{name='native_nonzero';status='ok';body='';native=23;expected=1},
    @{name='malformed_json';status='ok';body='not JSON';native=0;expected=1},
    @{name='empty_response';status='ok';body='';native=0;expected=1;empty=$true},
    @{name='invalid_status';status='unexpected';body='';native=0;expected=1}
  )
  foreach($case in $cases) {
    $value=if($case.empty){''}elseif($case.body){$case.body}else{@{code='DIAGNOSTIC';checks=@(@{name='fixture';status=$case.status})}|ConvertTo-Json -Compress -Depth 5}
    $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($value))
    $program="import base64,sys;sys.stderr.write('diagnostic stderr warning\n');print(base64.b64decode('$encoded').decode('utf-8'));sys.exit($($case.native))"
    Set-Content -LiteralPath (Join-Path $root 'client.py') -Value $program -Encoding UTF8
    $output=& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptsDir 'selfcheck.ps1') -InstallDir $root -PythonPath $PythonPath
    $actual=$LASTEXITCODE
    $rows += [pscustomobject]@{name=$case.name;passed=($actual -eq $case.expected);expected=$case.expected;actual=$actual;detail=($output -join "`n")}
  }
  # PowerShell 5 uses ASCII for pipes unless OutputEncoding is set explicitly.
  $env:PYTHONUTF8='1'
  $OutputEncoding=[System.Text.UTF8Encoding]::new($false)
  Set-Content -LiteralPath (Join-Path $root 'question.txt') -Value 'WINDOWS_UTF8:中文问题' -Encoding UTF8
  $encodedRoot=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($ClientRoot))
  $program=@"
import sys,base64
sys.path.insert(0,base64.b64decode('$encodedRoot').decode('utf-8'))
from app import peer_client
async def capture(args): return {'stdin':args.question.strip()}
peer_client.run=capture
sys.argv=['client','--question-stdin']
peer_client.main()
"@
  Set-Content -LiteralPath (Join-Path $root 'client.py') -Value $program -Encoding UTF8
  $value=Get-Content -LiteralPath (Join-Path $root 'question.txt') -Raw -Encoding UTF8 | & $PythonPath (Join-Path $root 'client.py')
  $nativeExit=$LASTEXITCODE
  $inputResult=$value | ConvertFrom-Json
  $rows += [pscustomobject]@{name='powershell_utf8_question_stdin';passed=($nativeExit -eq 0 -and [String]::Equals($inputResult.stdin,'WINDOWS_UTF8:中文问题',[StringComparison]::Ordinal));detail=$inputResult.stdin}
  $rows | ConvertTo-Json -Depth 5
  if(@($rows|Where-Object { -not $_.passed }).Count){exit 1}
  exit 0
} finally {
  if(Test-Path -LiteralPath $root){Remove-Item -LiteralPath $root -Recurse -Force}
}
