# Admin operation: dedicated non-admin SSH forwarding account; preview by default.
param(
 [Parameter(Mandatory=$true)][string]$PublicKey,
 [Parameter(Mandatory=$true)][string]$SourceAddress,
 [string]$InterfaceAlias='WLAN',
 [switch]$Apply
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$user='companyagentrelay'
$rule='CompanyAgent-LanRelay-SSH'
$dir=Join-Path $env:ProgramData 'CompanyAgentLanRelay'
$config=Join-Path $env:ProgramData 'ssh\sshd_config'
$image=[Environment]::ExpandEnvironmentVariables([string](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\sshd').ImagePath)
if($image -notmatch '^"?(.+\\sshd\.exe)"?$'){throw 'Unsupported installed SSH service executable'}
$sshd=$Matches[1]
if(-not (Test-Path -LiteralPath $sshd -PathType Leaf)){throw 'Installed SSH executable missing'}
if($PublicKey -notmatch '^ssh-ed25519 [A-Za-z0-9+/]{60,}={0,3}( [A-Za-z0-9._-]+)?$'){throw 'An Ed25519 public key is required'}
$ip=$null
if(-not [Net.IPAddress]::TryParse($SourceAddress,[ref]$ip) -or $ip.AddressFamily -ne 'InterNetwork' -or $ip.ToString() -ne $SourceAddress -or $SourceAddress -notmatch '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)'){throw 'One private LAN source address required'}
if($InterfaceAlias -notmatch '^[A-Za-z0-9 _.-]{1,64}$'){throw 'Invalid physical interface alias'}
$adapter=Get-NetAdapter -Name $InterfaceAlias
if($adapter.Virtual -or -not $adapter.HardwareInterface){throw 'Physical interface required'}
$original=[IO.File]::ReadAllText($config)
if((Get-LocalUser -Name $user -ErrorAction SilentlyContinue) -or (Test-Path $dir) -or (Get-NetFirewallRule -Name $rule -ErrorAction SilentlyContinue) -or $original.Contains('CompanyAgent-LanRelay')){throw 'Existing relay preserved; inspect its receipt before reconfiguration'}
if(-not $Apply){@{apply=$false;account=$user;admin=$false;source=$SourceAddress;interface=$InterfaceAlias;listener='127.0.0.1:24222';password_authentication=$false;command_sessions=$false} | ConvertTo-Json;exit 0}
$admin=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Administrator required'}
New-Item -ItemType Directory $dir | Out-Null
$backup=Join-Path $dir 'sshd_config.before'
Copy-Item $config $backup
$created=$false
$changed=$false
try{
 $random=[byte[]]::new(36);[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($random)
 $password=ConvertTo-SecureString ('Aa1!'+[Convert]::ToBase64String($random)) -AsPlainText -Force
 $account=New-LocalUser -Name $user -Password $password -AccountNeverExpires -PasswordNeverExpires -UserMayNotChangePassword -Description 'CompanyAgent managed LAN-only SSH reverse relay'
 $created=$true
 Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $user
 $keys=Join-Path $dir 'authorized_keys'
 $line='restrict,port-forwarding,from="'+$SourceAddress+'",permitlisten="127.0.0.1:24222" '+$PublicKey.Trim()
 [IO.File]::WriteAllText($keys,$line+"`n",[Text.UTF8Encoding]::new($false))
 # Restrict the entire managed directory; only this standard user may read its key.
 & icacls.exe $dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' ('*'+$account.SID.Value+':(OI)(CI)RX') | Out-Null
 if($LASTEXITCODE -ne 0){throw 'Directory ACL failed'}
 $block=@"

# CompanyAgent-LanRelay: isolated standard account; no command/session channels.
Match all
Match User $user
    AuthorizedKeysFile C:/ProgramData/CompanyAgentLanRelay/authorized_keys
    AuthenticationMethods publickey
    PasswordAuthentication no
    AllowTcpForwarding remote
    PermitListen 127.0.0.1:24222
    PermitOpen none
    GatewayPorts no
    AllowAgentForwarding no
    PermitTTY no
    MaxSessions 0
    ForceCommand cmd.exe /c exit 1
Match all
"@
 $candidate=Join-Path $dir 'sshd_config.candidate'
 [IO.File]::WriteAllText($candidate,$original+$block,[Text.UTF8Encoding]::new($false))
 & $sshd -t -f $candidate
 if($LASTEXITCODE -ne 0){throw 'Candidate SSH configuration invalid'}
 Copy-Item $candidate $config -Force;$changed=$true
 New-NetFirewallRule -Name $rule -DisplayName 'CompanyAgent restricted physical LAN relay' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22 -RemoteAddress $SourceAddress -InterfaceAlias $InterfaceAlias -Profile Any | Out-Null
 Restart-Service sshd
 $receipt=@{schema_version=1;account=$user;account_sid=$account.SID.Value;admin=$false;source=$SourceAddress;interface=$InterfaceAlias;listener='127.0.0.1:24222';password_saved=$false;password_authentication=$false;command_sessions=$false;backup=$backup;config_before_sha256=(Get-FileHash $backup).Hash;config_installed_sha256=(Get-FileHash $config).Hash;created_at=[DateTime]::UtcNow.ToString('o')}
 [IO.File]::WriteAllText((Join-Path $dir 'receipt.json'),($receipt | ConvertTo-Json),[Text.UTF8Encoding]::new($false))
 $receipt | ConvertTo-Json
}catch{
 $failure=$_
 if($changed){Copy-Item $backup $config -Force;Restart-Service sshd}
 Remove-NetFirewallRule -Name $rule -ErrorAction SilentlyContinue
 if($created){Remove-LocalUser -Name $user}
 # Preserve the backup and failed attempt for inspection; no recursive cleanup.
 throw $failure
}finally{
 $password=$null;$random=$null
}
