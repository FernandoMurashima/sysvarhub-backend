$ErrorActionPreference = 'Stop'
$WindowsRoot = Split-Path $PSScriptRoot -Parent
. (Join-Path $WindowsRoot 'mysql-backup.ps1')

function New-TestHubState {
    return [pscustomobject]@{
        partial_revokes=0; version='8.0.46'; port=3307; basedir=$script:mysqlRoot; datadir=(Join-Path $script:dataRoot 'mysql\data'); log_bin=1; binlog_format='ROW'; server_uuid='11111111-1111-4111-8111-111111111111'; server_id=1; account='sysvarhub_backup@127.0.0.1'; database='sysvarhub_db'
        schemas=@('mysql','information_schema','performance_schema','sys','sysvarhub_db'); tables=55; non_innodb=0; bytes=1024; routines=0; events=0
    }
}
function Get-TestHubIdentity {
    return [pscustomobject]@{hub_uuid='22222222222242228222222222222222';empresa_id=1;loja_id=10;retaguarda_hub_id=20}
}
function Get-TestHubGrants {
    return 'GRANT RELOAD, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO `sysvarhub_backup`@`127.0.0.1`' + "`n" + 'GRANT SELECT, SHOW VIEW, TRIGGER ON `sysvarhub\_db`.* TO `sysvarhub_backup`@`127.0.0.1`'
}

Describe 'Comandos e coordenadas do backup completo' {
    It 'fixa conta, host, porta e somente o banco Hub sem segredo em argumentos' {
        $args = Get-HubMySqlDumpArguments 'C:\Hub Data\config\mysql-backup.cnf' 'C:\Hub Data\backup\temporario.sql'
        $args[0] | Should Be '--defaults-file=C:\Hub Data\config\mysql-backup.cnf'
        foreach ($flag in @('--host=127.0.0.1','--port=3307','--protocol=TCP','--user=sysvarhub_backup','--single-transaction','--quick','--source-data=2','--no-tablespaces','--set-gtid-purged=OFF','--databases','sysvarhub_db')) {
            ($args -contains $flag) | Should Be $true
        }
        ($args -join ' ') | Should Not Match '3306|root|--password|all-databases|flush-logs|delete-source-logs|lock-all-tables'
        $args[-1] | Should Be 'sysvarhub_db'
    }
    It 'preserva espacos, aspas e barras no quoting Win32' {
        ConvertTo-HubNativeArgument 'C:\Hub Data\backup\dump.sql' | Should Be '"C:\Hub Data\backup\dump.sql"'
        ConvertTo-HubNativeArgument 'a"b\' | Should Be '"a\"b\\"'
    }
    It 'extrai coordenadas do snapshot da sintaxe atual' {
        $path = Join-Path $TestDrive 'atual.sql'
        Set-Content $path "-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='sysvarhub-bin.000007', SOURCE_LOG_POS=157;"
        $coords = Get-HubBackupCoordinates $path
        $coords.file | Should Be 'sysvarhub-bin.000007'
        $coords.position | Should Be 157
    }
    It 'aceita a sintaxe MASTER ainda emitida por versoes da serie 8.0' {
        $path = Join-Path $TestDrive 'anterior.sql'
        Set-Content $path "-- CHANGE MASTER TO MASTER_LOG_FILE='sysvarhub-bin.000008', MASTER_LOG_POS=194;"
        (Get-HubBackupCoordinates $path).position | Should Be 194
    }
    It 'rejeita coordenadas ausentes, caminho indevido e posicao invalida' {
        $path = Join-Path $TestDrive 'invalido.sql'
        foreach ($line in @('-- sem coordenadas',"-- CHANGE MASTER TO MASTER_LOG_FILE='../outro.000001', MASTER_LOG_POS=194;", "-- CHANGE MASTER TO MASTER_LOG_FILE='sysvarhub-bin.000001', MASTER_LOG_POS=0;")) {
            Set-Content $path $line
            { Get-HubBackupCoordinates $path } | Should Throw 'sem coordenadas'
        }
    }
}

Describe 'Seguranca da credencial e dos caminhos' {
    BeforeEach {
        $script:credentialPath = Join-Path $TestDrive ([guid]::NewGuid().ToString('N') + '.cnf')
        @('[client]','user=sysvarhub_backup','password=credencial-ficticia','host=127.0.0.1','port=3307','protocol=TCP') | Set-Content $credentialPath
        $script:protectedAcl = [pscustomobject]@{AreAccessRulesProtected=$true}
        $protectedAcl | Add-Member ScriptMethod GetAccessRules { param($a,$b,$c) @([pscustomobject]@{AccessControlType='Allow';IdentityReference=[pscustomobject]@{Value='S-1-5-18'}}) }
        Mock Get-Acl { $script:protectedAcl }
    }
    It 'aceita somente arquivo dedicado protegido' { { Assert-HubBackupCredential $credentialPath } | Should Not Throw }
    It 'bloqueia credencial ausente' { { Assert-HubBackupCredential ($credentialPath + '.ausente') } | Should Throw 'ausente' }
    It 'rejeita root mesmo que o arquivo tenha senha' {
        (Get-Content $credentialPath) -replace 'user=sysvarhub_backup','user=root' | Set-Content $credentialPath
        { Assert-HubBackupCredential $credentialPath } | Should Throw 'exclusivamente'
    }
    It 'rejeita porta DEV e configuracoes adicionais' {
        (Get-Content $credentialPath) -replace 'port=3307','port=3306' | Set-Content $credentialPath
        { Assert-HubBackupCredential $credentialPath } | Should Throw 'exclusivamente'
        Add-Content $credentialPath '!include outro.cnf'
        { Assert-HubBackupCredential $credentialPath } | Should Throw 'Formato'
    }
    It 'rejeita arquivo com heranca de ACL' {
        $protectedAcl.AreAccessRulesProtected = $false
        { Assert-HubBackupCredential $credentialPath } | Should Throw 'ACL restrita'
    }
    It 'rejeita acesso de outros usuarios ao segredo' {
        $protectedAcl | Add-Member ScriptMethod GetAccessRules { param($a,$b,$c) @([pscustomobject]@{AccessControlType='Allow';IdentityReference=[pscustomobject]@{Value='S-1-1-0'}}) } -Force
        { Assert-HubBackupCredential $credentialPath } | Should Throw 'acesso nao autorizado'
    }
    It 'rejeita destinos UNC e relativos' {
        { Assert-HubBackupLocalPath '\\servidor\backup' } | Should Throw 'local absoluto'
        { Assert-HubBackupLocalPath 'backup\local' } | Should Throw 'local absoluto'
    }
}

Describe 'Identidade do servico oficial' {
    BeforeEach {
        $script:service = [pscustomobject]@{Name='SysvarHubMySQL';State='Running';PathName='"C:\Program Files\Sysvar Hub\mysql\bin\mysqld.exe" --defaults-file="C:\ProgramData\SysvarHub\config\my.ini" SysvarHubMySQL'}
        Mock Get-CimInstance { $script:service }
    }
    It 'valida servico e arquivo de configuracao da instalacao' {
        { Assert-HubBackupService 'C:\Program Files\Sysvar Hub\mysql' 'C:\ProgramData\SysvarHub' } | Should Not Throw
    }
    It 'bloqueia MySQL80 e servico parado' {
        $service.Name = 'MySQL80'
        { Assert-HubBackupService 'C:\Program Files\Sysvar Hub\mysql' 'C:\ProgramData\SysvarHub' } | Should Throw 'DEV'
        $service.Name = 'SysvarHubMySQL'; $service.State = 'Stopped'
        { Assert-HubBackupService 'C:\Program Files\Sysvar Hub\mysql' 'C:\ProgramData\SysvarHub' } | Should Throw 'DEV'
    }
    It 'rejeita defaults-file ou executavel de outra instalacao' {
        $service.PathName = '"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqld.exe" --defaults-file="C:\outro.ini"'
        { Assert-HubBackupService 'C:\Program Files\Sysvar Hub\mysql' 'C:\ProgramData\SysvarHub' } | Should Throw 'oficial'
    }
}

Describe 'Preflight SQL somente de leitura' {
    BeforeEach {
        $script:mysqlRoot = 'C:\Hub\mysql'; $script:dataRoot = 'C:\HubData'
        $script:state = New-TestHubState
        $script:identity = Get-TestHubIdentity
        $script:grants = Get-TestHubGrants
        Mock Get-HubMySqlBinaryVersion { [version]'8.0.46' }
        Mock Get-HubBackupQuery {
            switch -Regex ($Sql) {
                '^SHOW GRANTS' { return $script:grants }
                '^SHOW MASTER STATUS' { return "sysvarhub-bin.000007`t300`t`t`t" }
                '^SELECT JSON_OBJECT.*FROM core_hubconfig;' { return ($script:identity | ConvertTo-Json -Compress) }
                default { return ($script:state | ConvertTo-Json -Depth 4 -Compress) }
            }
        }
    }
    It 'confirma conta, schema, servidor e loja identificada' {
        $result = Get-HubBackupServerState $mysqlRoot $dataRoot 'ficticio.cnf'
        $result.identity.loja_id | Should Be 10
        $result.server_uuid | Should Be $state.server_uuid
    }
    It 'rejeita servidor de versao diferente do pacote' {
        $state.version='8.0.45'
        { Get-HubBackupServerState $mysqlRoot $dataRoot 'ficticio.cnf' } | Should Throw 'incompativel'
    }
    It 'rejeita servidor compartilhado' {
        $state.schemas += 'outro_banco'
        { Get-HubBackupServerState $mysqlRoot $dataRoot 'ficticio.cnf' } | Should Throw 'compartilhada'
    }
    It 'rejeita conta root ou porta DEV na resposta oficial' {
        $state.port=3306
        { Get-HubBackupServerState $mysqlRoot $dataRoot 'ficticio.cnf' } | Should Throw '3307'
        $state.port=3307; $state.account='root@localhost'
        { Get-HubBackupServerState $mysqlRoot $dataRoot 'ficticio.cnf' } | Should Throw '3307'
    }
    It 'rejeita binlogs desabilitados ou formato diferente de ROW' {
        $state.log_bin=0
        { Get-HubBackupServerState $mysqlRoot $dataRoot 'ficticio.cnf' } | Should Throw 'Binlogs'
        $state.log_bin=1; $state.binlog_format='STATEMENT'
        { Get-HubBackupServerState $mysqlRoot $dataRoot 'ficticio.cnf' } | Should Throw 'Binlogs'
    }
    It 'rejeita dados em diretorio diferente' {
        $state.datadir='C:\OutraInstalacao\data'
        { Get-HubBackupServerState $mysqlRoot $dataRoot 'ficticio.cnf' } | Should Throw 'Diretorios'
    }
    It 'rejeita tabela nao transacional' {
        $state.non_innodb=1
        { Get-HubBackupServerState $mysqlRoot $dataRoot 'ficticio.cnf' } | Should Throw 'InnoDB'
    }
    It 'rejeita loja nao identificada' {
        $identity.loja_id=$null
        { Get-HubBackupServerState $mysqlRoot $dataRoot 'ficticio.cnf' } | Should Throw 'identificadas'
    }
    It 'rejeita grant amplo ou com curinga no nome do banco' {
        { Assert-HubBackupGrants 'GRANT ALL PRIVILEGES ON *.* TO usuario' } | Should Throw 'excessivos'
        { Assert-HubBackupGrants ((Get-TestHubGrants).Replace('\_','_')) } | Should Throw 'curinga'
    }
    It 'rejeita restricao parcial que poderia esconder outros bancos' {
        { Assert-HubBackupGrants ((Get-TestHubGrants) + "`nREVOKE SELECT ON outro.* FROM usuario") } | Should Throw 'nao homologadas'
    }
    It 'valida existencia e tamanho do arquivo de binlog que ancora o dump' {
        Mock Get-HubBackupQuery { "sysvarhub-bin.000007`t300`tNo" }
        { Assert-HubBackupCoordinates $mysqlRoot 'ficticio.cnf' ([pscustomobject]@{file='sysvarhub-bin.000007';position=157}) } | Should Not Throw
        { Assert-HubBackupCoordinates $mysqlRoot 'ficticio.cnf' ([pscustomobject]@{file='sysvarhub-bin.000007';position=301}) } | Should Throw 'indisponiveis'
    }
}

$script:compressImplementation = (Get-Command Compress-HubBackupSql).ScriptBlock
Describe 'Publicacao completa e falhas sem afetar backups anteriores' {
    BeforeEach {
        $script:caseRoot = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $script:installRoot = Join-Path $caseRoot 'app'
        $script:dataRoot = Join-Path $caseRoot 'state'
        $script:mysqlRoot = Join-Path $installRoot 'mysql'
        $script:backupRoot = Join-Path $dataRoot 'backup'
        $null = New-Item -ItemType Directory -Path $backupRoot -Force
        $script:previous = Join-Path $backupRoot 'full-anterior'
        $null = New-Item -ItemType Directory -Path $previous
        Set-Content -LiteralPath (Join-Path $previous 'anterior.gz') -Value 'backup valido anterior ficticio'
        $script:previousHash = (Get-FileHash (Join-Path $previous 'anterior.gz')).Hash
        $script:serverState = New-TestHubState
        $serverState | Add-Member NoteProperty identity (Get-TestHubIdentity)
        Mock Assert-HubBackupOperator { }
        Mock Assert-HubBackupService { }
        Mock Assert-HubMySqlToolchain { $script:mysqlRoot }
        Mock Assert-HubBackupCredential { }
        Mock Protect-SecretFile { }
        Mock Get-HubBackupServerState { $script:serverState }
        Mock Get-HubBackupFreeBytes { 10GB }
        Mock Get-HubBinlogPrefixAnchor { [pscustomobject]@{algorithm="MYSQL8_CRC32_PREFIX_V1";file=$Coordinate.file;position=$Coordinate.position;sha256=("a"*64)} }
        Mock Assert-HubBackupCoordinates { }
        Mock Compress-HubBackupSql { & $script:compressImplementation -SqlPath $SqlPath -ArchivePath $ArchivePath }
        Mock Invoke-HubMySqlProcess {
            $output = ($Arguments | Where-Object { $_ -like '--result-file=*' }).Substring('--result-file='.Length)
            [IO.File]::WriteAllText($output, "-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='sysvarhub-bin.000007', SOURCE_LOG_POS=157;`nCREATE DATABASE sysvarhub_db;`n-- Dump completed`n", (New-Object Text.UTF8Encoding($false)))
        }
    }
    AfterEach {
        (Get-FileHash (Join-Path $previous 'anterior.gz')).Hash | Should Be $previousHash
        @(Get-ChildItem -LiteralPath $backupRoot -Directory -Force | Where-Object { $_.Name -like '.incomplete-*' }).Count | Should Be 0
    }
    It 'publica gzip e manifesto validos juntos, com checksum e coordenadas' {
        $destination = Invoke-HubMySqlFullBackup $installRoot $dataRoot
        $manifest = Get-Content -LiteralPath (Join-Path $destination 'manifest.json') -Raw | ConvertFrom-Json
        $manifest.status | Should Be 'VALIDO'
        $manifest.installation.loja_id | Should Be 10
        $manifest.mysql.database | Should Be 'sysvarhub_db'
        $manifest.mysql.server_uuid | Should Be $serverState.server_uuid
        $manifest.binlog.file | Should Be 'sysvarhub-bin.000007'
        $manifest.binlog.position | Should Be 157
        $manifest.archive.sha256 | Should Be (Get-FileHash (Join-Path $destination $manifest.archive.name)).Hash.ToLowerInvariant()
        $manifest.archive.size_bytes | Should Be (Get-Item (Join-Path $destination $manifest.archive.name)).Length
        @(Get-ChildItem -LiteralPath $destination -File).Count | Should Be 2
        Test-Path (Join-Path $destination 'sysvarhub_db.sql') | Should Be $false
        Assert-MockCalled Protect-SecretFile -Scope It -Times 2 -Exactly
    }
    It 'nao sobrescreve backup anterior nem duas execucoes no mesmo instante' {
        $first = Invoke-HubMySqlFullBackup $installRoot $dataRoot
        $hash = (Get-FileHash (Join-Path $first 'sysvarhub_db.sql.gz')).Hash
        $second = Invoke-HubMySqlFullBackup $installRoot $dataRoot
        $first | Should Not Be $second
        (Get-FileHash (Join-Path $first 'sysvarhub_db.sql.gz')).Hash | Should Be $hash
    }
    It 'nao inicia dump quando credencial e recusada' {
        Mock Assert-HubBackupCredential { throw 'credencial recusada' }
        { Invoke-HubMySqlFullBackup $installRoot $dataRoot } | Should Throw 'credencial recusada'
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 0 -Exactly
    }
    It 'nao inicia dump sem espaco suficiente' {
        Mock Get-HubBackupFreeBytes { 1 }
        { Invoke-HubMySqlFullBackup $installRoot $dataRoot } | Should Throw 'Espaco'
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 0 -Exactly
    }
    It 'remove apenas staging quando o dump falha' {
        Mock Invoke-HubMySqlProcess { throw 'dump falhou' }
        { Invoke-HubMySqlFullBackup $installRoot $dataRoot } | Should Throw 'dump falhou'
        @(Get-ChildItem $backupRoot -Directory).Count | Should Be 1
    }
    It 'nao publica dump com coordenadas ausentes' {
        Mock Invoke-HubMySqlProcess {
            $output = ($Arguments | Where-Object { $_ -like '--result-file=*' }).Substring('--result-file='.Length)
            Set-Content $output '-- dump sem coordenadas'
        }
        { Invoke-HubMySqlFullBackup $installRoot $dataRoot } | Should Throw 'coordenadas'
        @(Get-ChildItem $backupRoot -Directory).Count | Should Be 1
    }
    It 'nao publica quando o binlog do snapshot desapareceu' {
        Mock Assert-HubBackupCoordinates { throw 'binlog indisponivel' }
        { Invoke-HubMySqlFullBackup $installRoot $dataRoot } | Should Throw 'binlog indisponivel'
        @(Get-ChildItem $backupRoot -Directory).Count | Should Be 1
    }
    It 'nao publica arquivo parcial se a compactacao falhar' {
        Mock Compress-HubBackupSql { Set-Content $ArchivePath 'parcial'; throw 'compactacao falhou' }
        { Invoke-HubMySqlFullBackup $installRoot $dataRoot } | Should Throw 'compactacao falhou'
        @(Get-ChildItem $backupRoot -Directory).Count | Should Be 1
    }
    It 'nao publica se identidade do servidor mudar durante dump' {
        $script:stateCalls=0
        Mock Get-HubBackupServerState {
            $script:stateCalls++
            $copy = $script:serverState | Select-Object *
            if ($script:stateCalls -gt 1) { $copy.server_uuid='33333333-3333-4333-8333-333333333333' }
            return $copy
        }
        { Invoke-HubMySqlFullBackup $installRoot $dataRoot } | Should Throw 'Identidade'
        @(Get-ChildItem $backupRoot -Directory).Count | Should Be 1
    }
    It 'bloqueia execucao simultanea sem iniciar outro dump' {
        $lock = [IO.File]::Open((Join-Path $backupRoot '.full-backup.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        try { { Invoke-HubMySqlFullBackup $installRoot $dataRoot } | Should Throw 'bloqueio local' } finally { $lock.Dispose() }
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 0 -Exactly
    }
    It 'nao publica backup se nao conseguir validar o checksum' {
        Mock Get-FileHash { [pscustomobject]@{Hash='invalido'} } -ParameterFilter { $LiteralPath -like '*sysvarhub_db.sql.gz' }
        { Invoke-HubMySqlFullBackup $installRoot $dataRoot } | Should Throw 'Checksum'
        @(Get-ChildItem $backupRoot -Directory).Count | Should Be 1
    }
    It 'nao apaga area temporaria preexistente em colisao de nomes' {
        $script:collision = $null
        Mock Test-Path {
            $script:collision = $LiteralPath
            $null = [IO.Directory]::CreateDirectory($LiteralPath)
            [IO.File]::WriteAllText((Join-Path $LiteralPath 'preservar.txt'), 'preexistente')
            return $true
        } -ParameterFilter { $LiteralPath -like '*\.incomplete-*' }
        try {
            { Invoke-HubMySqlFullBackup $installRoot $dataRoot } | Should Throw 'ja existe'
            [IO.File]::ReadAllText((Join-Path $script:collision 'preservar.txt')) | Should Be 'preexistente'
        } finally {
            if ($script:collision) {
                # Apenas os dois artefatos ficticios criados neste teste; sem remocao recursiva.
                [IO.File]::Delete((Join-Path $script:collision 'preservar.txt'))
                [IO.Directory]::Delete($script:collision)
            }
        }
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 0 -Exactly
    }

}

Describe 'Processo nativo sem MySQL real' {
    It 'oculta stderr e argumentos quando o executavel nao existe' {
        { Invoke-HubMySqlProcess -Executable 'C:\inexistente-hub-backup.exe' -Arguments @() } | Should Throw 'Ferramenta MySQL falhou'
    }
    It 'executa subprocesso ficticio com UTF-8 sem executar MySQL' {
        $program = Join-Path $TestDrive 'fake-output.ps1'
        [IO.File]::WriteAllText($program, "[Console]::OutputEncoding = New-Object Text.UTF8Encoding(`$false); [Console]::Out.Write('metadados-ficticios'); exit 0", (New-Object Text.UTF8Encoding($false)))
        Invoke-HubMySqlProcess -Executable (Join-Path $PSHOME 'powershell.exe') -Arguments @('-NoProfile','-NonInteractive','-File',$program) -InputSql 'SELECT ficticio;' | Should Be 'metadados-ficticios'
    }
    It 'isola credenciais de ambiente e login-path somente no processo filho' {
        $program = Join-Path $TestDrive 'fake-environment.ps1'
        Set-Content $program '[Console]::Out.Write(([string]::IsNullOrEmpty($env:MYSQL_PWD) -and $env:MYSQL_TEST_LOGIN_FILE -like "*sysvarhub-no-login-*" -and -not [IO.File]::Exists($env:MYSQL_TEST_LOGIN_FILE)))'
        $previous = [Environment]::GetEnvironmentVariable('MYSQL_PWD','Process')
        try {
            [Environment]::SetEnvironmentVariable('MYSQL_PWD','ficticio','Process')
            Invoke-HubMySqlProcess -Executable (Join-Path $PSHOME 'powershell.exe') -Arguments @('-NoProfile','-NonInteractive','-File',$program) | Should Be 'True'
            [Environment]::GetEnvironmentVariable('MYSQL_PWD','Process') | Should Be 'ficticio'
        } finally { [Environment]::SetEnvironmentVariable('MYSQL_PWD',$previous,'Process') }
    }
    It 'nao repassa stderr de processo ficticio com erro' {
        $program = Join-Path $TestDrive 'fake-error.ps1'
        Set-Content $program "[Console]::Error.Write('detalhe-nao-publicavel'); exit 1"
        try { Invoke-HubMySqlProcess -Executable (Join-Path $PSHOME 'powershell.exe') -Arguments @('-NoProfile','-NonInteractive','-File',$program); throw 'processo deveria falhar' } catch {
            $_.Exception.Message | Should Match 'Ferramenta MySQL falhou'
            $_.Exception.Message | Should Not Match 'detalhe-nao-publicavel'
        }
    }
    It 'encerra processo ficticio ao exceder timeout sem publicar arquivos' {
        $program = Join-Path $TestDrive 'fake-timeout.ps1'
        Set-Content $program 'Start-Sleep -Seconds 5'
        { Invoke-HubMySqlProcess -Executable (Join-Path $PSHOME 'powershell.exe') -Arguments @('-NoProfile','-NonInteractive','-File',$program) -TimeoutSeconds 1 } | Should Throw 'tempo limite'
    }
    It 'valida sintaxe sem executar instalador ou comando de backup' {
        foreach ($name in @('mysql-backup.ps1','backup-full.ps1','install-hub.ps1','build-installer.ps1')) {
            $errors=$null
            $null=[Management.Automation.Language.Parser]::ParseFile((Join-Path $WindowsRoot $name), [ref]$null, [ref]$errors)
            @($errors).Count | Should Be 0
        }
    }
}


Describe 'ACL real apenas em diretorio ficticio' {
    It 'protege arquivo ficticio sem depender de privilegios de auditoria' {
        $path = Join-Path $TestDrive 'private-file.txt'
        Set-Content $path 'conteudo ficticio'
        $original = [IO.File]::GetAccessControl($path, [Security.AccessControl.AccessControlSections]::Access)
        try {
            Protect-SecretFile $path
            $acl = Get-Acl -LiteralPath $path
            $acl.AreAccessRulesProtected | Should Be $true
            @($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])).Count | Should Be 2
        } finally {
            $restore = New-Object Security.AccessControl.FileSecurity
            $restore.SetSecurityDescriptorSddlForm($original.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access), [Security.AccessControl.AccessControlSections]::Access)
            [IO.File]::SetAccessControl($path, $restore)
        }
    }

    It 'aplica regras somente a SYSTEM e Administradores com heranca para os arquivos' {
        $path = Join-Path $TestDrive 'private-acl'
        $null = New-Item -ItemType Directory -Path $path
        $original = [IO.Directory]::GetAccessControl($path, [Security.AccessControl.AccessControlSections]::Access)
        try {
            Protect-SecretFile $path
            $acl = Get-Acl -LiteralPath $path
            $acl.AreAccessRulesProtected | Should Be $true
            $rules = @($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
            $rules.Count | Should Be 2
            foreach ($rule in $rules) {
                (@('S-1-5-18','S-1-5-32-544') -contains $rule.IdentityReference.Value) | Should Be $true
                $rule.InheritanceFlags.ToString() | Should Match 'ObjectInherit'
            }
        } finally {
            $restore = New-Object Security.AccessControl.DirectorySecurity
            $restore.SetSecurityDescriptorSddlForm($original.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access), [Security.AccessControl.AccessControlSections]::Access)
            [IO.Directory]::SetAccessControl($path, $restore)
            (Get-Acl -LiteralPath $path).AreAccessRulesProtected | Should Be $original.AreAccessRulesProtected
        }
    }
}
