<# Sign only release scripts with an existing enterprise Code Signing certificate. #>
param(
  [Parameter(Mandatory=$true)][string]$CertificateThumbprint,
  [string]$TimestampServer='',
  [string]$ReleaseDirectory=$PSScriptRoot
)
$ErrorActionPreference='Stop'
$thumbprint=$CertificateThumbprint.Replace(' ','').ToUpperInvariant()
if($thumbprint -notmatch '^[0-9A-F]{40}$'){throw 'Invalid certificate thumbprint'}
$cert=Get-Item -LiteralPath ('Cert:\CurrentUser\My\'+$thumbprint)
if(-not $cert.HasPrivateKey -or $cert.NotAfter -lt [DateTime]::Now -or $cert.NotBefore -gt [DateTime]::Now){throw 'Valid signing certificate and private key required'}
if(-not @($cert.EnhancedKeyUsageList | Where-Object {$_.ObjectId.Value -eq '1.3.6.1.5.5.7.3.3'}).Count){throw 'Code Signing EKU required'}
foreach($name in @('install_windows.ps1','selfcheck.ps1','configure_windows_worker.ps1','configure_windows_service.ps1','install_windows_msi.ps1','windows_recovery_check.ps1')){
  $file=Join-Path $ReleaseDirectory $name
  $args=@{FilePath=$file;Certificate=$cert;HashAlgorithm='SHA256'}
  if($TimestampServer){$args.TimestampServer=$TimestampServer}
  $signed=Set-AuthenticodeSignature @args
  if($signed.Status -ne 'Valid'){throw ('Signing not valid: '+$name)}
  [ordered]@{file=$name;status=[string]$signed.Status;thumbprint=$thumbprint} | ConvertTo-Json -Compress
}
