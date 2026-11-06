<# Verify the MSI using a separately provisioned publisher key before msiexec.
   MSI installs public software. Identity enrollment is a separate owner action.
#>
param(
 [Parameter(Mandatory=$true)][string]$Package,
 [Parameter(Mandatory=$true)][string]$Signature,
 [Parameter(Mandatory=$true)][string]$TrustedKey,
 [Parameter(Mandatory=$true)][string]$PythonPath,
 [string]$LogPath = "$env:TEMP\CompanyAgentMsi-$([Guid]::NewGuid().ToString('N')).log",
 [switch]$Remove
)
$ErrorActionPreference='Stop'
$stage=Join-Path $env:TEMP ('CompanyAgentMsiVerify-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
 $verified=Join-Path $stage 'client.msi'
 Copy-Item -LiteralPath $Package -Destination $verified
 $verifier=Join-Path $PSScriptRoot '..\packaging\client\package_trust.py'
 if(-not (Test-Path $verifier)){$verifier=Join-Path $PSScriptRoot 'package_trust.py'}
 & $PythonPath $verifier --package $verified --signature $Signature --trusted-key $TrustedKey
 if($LASTEXITCODE){throw 'MSI publisher verification failed'}
 if(-not $Remove -and (Test-Path (Join-Path $env:LOCALAPPDATA 'CompanyAgentOfflineClient')) -and
    -not (Test-Path 'HKCU:\Software\CompanyAgent\OfflineClient')){
   throw 'Existing unmanaged software directory preserved; choose a reviewed migration first.'
 }
 $mode=if($Remove){'/x'}else{'/i'}
 # Native invocation preserves argument boundaries, including paths with spaces.
 & msiexec.exe $mode $verified /qn /norestart /L*v $LogPath | Out-Null
 $code=$LASTEXITCODE
 if($code -notin @(0,3010)){throw "MSI operation failed: $code; log: $LogPath"}
 @{publisher_verified=$true;exit_code=$code;restart_required=($code -eq 3010);log=$LogPath;software_only=$true;removed=[bool]$Remove}|ConvertTo-Json
} finally {
 if(Test-Path $stage){Remove-Item -LiteralPath $stage -Recurse -Force}
}
