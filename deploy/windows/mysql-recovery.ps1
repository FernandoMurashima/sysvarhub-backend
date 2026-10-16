# Importacao sem efeitos colaterais; execucao somente assistida e explicita.
. (Join-Path $PSScriptRoot 'mysql-backup.ps1')

function Assert-HubRecoveryStopped {
    $service = Get-Service -Name 'SysvarHub' -ErrorAction SilentlyContinue
    if ($service -and $service.Status -ne 'Stopped') { throw 'SysvarHub deve estar parado pelo suporte antes da recuperacao.' }
}

function Initialize-HubRecoveryMode {
    param([string]$ProgramDataRoot, [switch]$SupportConfirmed)
    if (-not $SupportConfirmed) { throw 'Preparacao exige confirmacao explicita do suporte.' }
    Assert-HubBackupOperator
    $ProgramDataRoot = Assert-HubBackupLocalPath $ProgramDataRoot
    Assert-HubRecoveryStopped
    $config = Join-Path $ProgramDataRoot 'config'
    $null = New-Item -ItemType Directory -Path $config -Force
    Protect-SecretFile $config
    $marker = Join-Path $config 'recovery-block.json'
    if (-not (Test-Path -LiteralPath $marker)) {
        Write-HubBackupJson $marker ([ordered]@{version=1;status='BLOQUEADO_SUPORTE';created_at_utc=[DateTime]::UtcNow.ToString('o')})
        Protect-SecretFile $marker
    }
    $service = Get-Service -Name 'SysvarHub' -ErrorAction SilentlyContinue
    if ($service) { Set-Service -Name 'SysvarHub' -StartupType Disabled -ErrorAction Stop }
    return $marker
}

function Assert-HubRecoveryPrivatePackage {
    param([string]$Directory)
    $null=Assert-HubBackupLocalPath $Directory
    $acl=Get-Acl -LiteralPath $Directory
    if (-not $acl.AreAccessRulesProtected) { throw 'Pacote exige diretorio privado com ACL protegida.' }
    $items=@((Get-Item -LiteralPath (Join-Path $Directory 'pacote.json'))) + @(Get-ChildItem -LiteralPath $Directory -File -Filter '*.sql.gz')
    $acls=@($acl)
    foreach ($item in $items) {
        $null=Assert-HubBackupLocalPath $item.FullName
        $acls+=Get-Acl -LiteralPath $item.FullName
    }
    foreach ($itemAcl in $acls) {
        foreach ($rule in $itemAcl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin @('S-1-5-18','S-1-5-32-544')) { throw 'Pacote com acesso nao autorizado.' }
        }
    }
}

function Get-HubRecoveryConnectionArguments {
    param([string]$CredentialFile)
    return @("--defaults-file=$CredentialFile",'--host=127.0.0.1','--port=3307','--protocol=TCP','--user=sysvarhub_restore','--connect-timeout=5','--default-character-set=utf8mb4')
}

function Assert-HubRecoveryGrants {
    param([string]$Grants,[bool]$PartialRevokes)
    $globals=@(); $schema=@()
    foreach ($line in ($Grants -split '\r?\n' | Where-Object { $_.Trim() })) {
        if ($line -notmatch '^GRANT (.+) ON (.+) TO ' -or $line -match 'WITH GRANT OPTION') { throw 'Permissoes da conta assistida nao homologadas.' }
        $privileges=@($Matches[1] -split ', '); $raw=$Matches[2].Replace('`','')
        if ($raw -eq '*.*') {
            foreach ($privilege in $privileges) {
                if ($privilege -notin @('USAGE','PROCESS','SHOW DATABASES','SESSION_VARIABLES_ADMIN','REPLICATION_APPLIER')) { throw 'Conta de restauracao com privilegios globais excessivos.' }
            }
            $globals += $privileges
        } elseif ($raw -eq $(if ($PartialRevokes) { 'sysvarhub_db.*' } else { 'sysvarhub\_db.*' })) {
            $schema += $privileges
        } else { throw 'Conta de restauracao nao pode acessar outros bancos ou herdar papeis.' }
    }
    foreach ($privilege in @('PROCESS','SHOW DATABASES','SESSION_VARIABLES_ADMIN','REPLICATION_APPLIER')) {
        if ($globals -notcontains $privilege) { throw 'Conta assistida sem privilegios globais minimos.' }
    }
    if ($schema -notcontains 'ALL PRIVILEGES') {
        foreach ($privilege in @('SELECT','INSERT','UPDATE','DELETE','CREATE','ALTER','DROP','INDEX','LOCK TABLES','REFERENCES','TRIGGER','CREATE VIEW','SHOW VIEW','CREATE TEMPORARY TABLES')) {
            if ($schema -notcontains $privilege) { throw 'Conta assistida sem privilegios de restauracao no banco Hub.' }
        }
    }
}

function Invoke-HubRecoveryRuntime {
    param([string]$InstallRoot,[string]$PlanPath,[switch]$Execute,[string]$Confirmation)
    $exe=Assert-HubBackupLocalPath (Join-Path $InstallRoot 'runtime\SysvarHubService.exe')
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Runtime de recuperacao oficial ausente.' }
    $arguments=@('recover','--plan',$PlanPath)
    if ($Execute) { $arguments+=@('--execute','--confirmation',$Confirmation) }
    $output=@(& $exe @arguments 2>$null)
    $exitCode=$LASTEXITCODE
    try { $result=([string]($output | Select-Object -Last 1) | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw 'Diagnostico de recuperacao invalido; destino permanece bloqueado.' }
    if ($exitCode -ne 0) {
        $code=if ($result.codigo -cmatch '^[A-Z0-9_]{1,80}$') { $result.codigo } else { 'RECOVERY_FAILED' }
        throw ('Recuperacao interrompida: ' + $code + '; destino permanece bloqueado para suporte.')
    }
    return $result
}

function Invoke-HubMySqlRecovery {
    param(
        [string]$PackageDirectory,[string]$ManifestSha256,[guid]$PointUuid,
        [long]$EmpresaId,[long]$LojaId,[guid]$InstallationUuid,[guid]$SourceServerUuid,
        [string]$InstallRoot='C:\Program Files\Sysvar Hub',[string]$ProgramDataRoot='C:\ProgramData\SysvarHub',
        [switch]$Execute,[string]$Confirmation,[switch]$ResumeAfterSupport,
        [ValidateRange(60,86400)][int]$TimeoutSeconds=7200
    )
    $ErrorActionPreference='Stop'
    Assert-HubBackupOperator
    if ($ManifestSha256 -notmatch '^[a-f0-9]{64}$' -or $PointUuid -eq [guid]::Empty -or $InstallationUuid -eq [guid]::Empty -or $SourceServerUuid -eq [guid]::Empty -or $EmpresaId -lt 1 -or $LojaId -lt 1) { throw 'Identidade/ponto/hash oficial devem ser informados pelo suporte.' }
    $expected='RESTAURAR:' + $PointUuid.ToString() + ':' + $ManifestSha256
    if ($Execute -and $Confirmation -cne $expected) { throw 'Execucao exige confirmacao explicita vinculada ao ponto e manifesto.' }
    $InstallRoot=Assert-HubBackupLocalPath $InstallRoot
    $ProgramDataRoot=Assert-HubBackupLocalPath $ProgramDataRoot
    $PackageDirectory=Assert-HubBackupLocalPath $PackageDirectory
    Assert-HubRecoveryPrivatePackage $PackageDirectory
    $marker=Join-Path $ProgramDataRoot 'config\recovery-block.json'
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { throw 'Prepare primeiro o modo de recuperacao; nao ha bloqueio persistente.' }
    Assert-HubRecoveryStopped
    $service=Get-Service -Name 'SysvarHub' -ErrorAction SilentlyContinue
    if ($service -and $service.StartType -ne 'Disabled') { throw 'SysvarHub precisa estar desativado durante a recuperacao.' }
    $mysqlRoot=Join-Path $InstallRoot 'mysql'
    Assert-HubBackupService $mysqlRoot $ProgramDataRoot
    $null=Assert-HubMySqlToolchain $mysqlRoot
    $credential=Join-Path $ProgramDataRoot 'config\mysql-restore.cnf'
    Assert-HubBackupCredential $credential -ExpectedUser 'sysvarhub_restore'
    $connectionArgs=(Get-HubRecoveryConnectionArguments $credential) + @('--batch','--raw','--skip-column-names')
    $grants=Invoke-HubMySqlProcess -Executable (Join-Path $mysqlRoot 'bin\mysql.exe') -Arguments $connectionArgs -InputSql 'SHOW GRANTS FOR CURRENT_USER();'
    $partial=(Invoke-HubMySqlProcess -Executable (Join-Path $mysqlRoot 'bin\mysql.exe') -Arguments $connectionArgs -InputSql 'SELECT @@GLOBAL.partial_revokes;').Trim()
    if ($partial -notin @('0','1')) { throw 'Escopo de privilegios desconhecido.' }
    Assert-HubRecoveryGrants $grants -PartialRevokes ($partial -eq '1')
    $version=Get-HubMySqlBinaryVersion (Join-Path $mysqlRoot 'bin\mysqld.exe')
    $plan=[ordered]@{version=1;install_root=$InstallRoot;program_data=$ProgramDataRoot;package_dir=$PackageDirectory;manifest_sha256=$ManifestSha256;point_uuid=$PointUuid.ToString();empresa_id=$EmpresaId;loja_id=$LojaId;installation_uuid=$InstallationUuid.ToString();source_server_uuid=$SourceServerUuid.ToString();mysql_version=$version.ToString();timeout_seconds=$TimeoutSeconds;resume_after_support=[bool]$ResumeAfterSupport}
    # Sem segredos: arquivo exclusivo, protegido antes de gravar o plano.
    $path=Join-Path $ProgramDataRoot ('config\.recovery-plan-' + [guid]::NewGuid().ToString('N') + '.json')
    $file=[IO.File]::Open($path,[IO.FileMode]::CreateNew); $file.Dispose()
    try {
        Protect-SecretFile $path
        [IO.File]::WriteAllText($path,($plan | ConvertTo-Json -Depth 8),(New-Object Text.UTF8Encoding($false)))
        return Invoke-HubRecoveryRuntime $InstallRoot $path -Execute:$Execute -Confirmation $Confirmation
    } finally { if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) } }
}
