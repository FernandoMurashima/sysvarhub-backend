# Helpers sem efeitos colaterais ao importar. Nunca conectam ao MySQL DEV.
function Get-HubMySqlBinaryVersion {
    param([string]$Path)
    $info = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($Path)
    if ($info.ProductName -ne "MySQL Server") {
        throw "Binario nao reconhecido como MySQL Server: $([IO.Path]::GetFileName($Path))."
    }
    return [version]("{0}.{1}.{2}" -f $info.FileMajorPart, $info.FileMinorPart, $info.FileBuildPart)
}

function Assert-HubMySqlToolchain {
    param([string]$Root)
    if (-not $Root -or -not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "MySqlRoot invalido. Informe uma distribuicao oficial MySQL 8.0 extraida."
    }
    $serverVersion = $null
    foreach ($name in @("mysqld.exe", "mysql.exe", "mysqldump.exe", "mysqlbinlog.exe")) {
        $path = Join-Path $Root "bin\$name"
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Ferramenta MySQL obrigatoria ausente: $name."
        }
        $version = Get-HubMySqlBinaryVersion $path
        if ($version.Major -ne 8 -or $version.Minor -ne 0 -or $version.Build -lt 26) {
            throw "Versao MySQL nao suportada em $name. Exigido MySQL 8.0.26 ou posterior da serie 8.0."
        }
        if ($null -eq $serverVersion) { $serverVersion = $version }
        if ($version -ne $serverVersion) {
            throw "Ferramenta $name incompativel com mysqld.exe. Use a mesma versao 8.0.x em todos os binarios."
        }
    }
    return (Resolve-Path -LiteralPath $Root).Path
}

function Test-HubMySqlNewInstallation {
    param([string]$ConfigRoot, [string]$MysqlData, [bool]$ServiceExists)
    if ($ServiceExists) { return $false }
    foreach ($name in @("my.ini", "sysvarhub.env", "mysql-admin.cnf", "mysql-backup.cnf")) {
        if (Test-Path -LiteralPath (Join-Path $ConfigRoot $name)) { return $false }
    }
    if (Test-Path -LiteralPath $MysqlData) {
        if (Get-ChildItem -LiteralPath $MysqlData -Force | Select-Object -First 1) { return $false }
    }
    return $true
}

function Get-HubMySqlNewConfig {
    param(
        [string]$InstallRoot,
        [string]$MysqlData,
        [ValidateRange(1, 2147483647)][int]$ServerId = 1,
        [ValidateRange(1, 365)][int]$RetentionDays = 30
    )
    return @(
        "[mysqld]",
        "basedir=$($InstallRoot.Replace('\','/'))/mysql",
        "datadir=$($MysqlData.Replace('\','/'))",
        "port=3307",
        "bind-address=127.0.0.1",
        "character-set-server=utf8mb4",
        "collation-server=utf8mb4_unicode_ci",
        "default-storage-engine=INNODB",
        "innodb_flush_log_at_trx_commit=1",
        "log_bin=$($MysqlData.Replace('\','/'))/sysvarhub-bin",
        "binlog_format=ROW",
        "server_id=$ServerId",
        "sync_binlog=1",
        "binlog_expire_logs_seconds=$($RetentionDays * 86400)",
        "[client]",
        "default-character-set=utf8mb4"
    )
}

function Initialize-HubMySqlBackupAccount {
    param([bool]$NewInstallation, [string]$CredentialFile)
    if (-not $NewInstallation) {
        Write-Warning "MySQL existente: configuracao e conta de backup preservadas. Backup requer validacao/adequacao assistida de versao, exclusividade, binlogs, retencao e privilegios."
        return $false
    }
    if (Test-Path -LiteralPath $CredentialFile) {
        throw "Credencial de backup preexistente: requer adequacao assistida."
    }
    # REPLICATION SLAVE e global: so provisionar na instancia nova e exclusiva.
    $otherDatabases = @(Invoke-MySqlCommand -Arguments ((Get-MySqlAdminArgs) + @("--batch", "--skip-column-names")) `
        -InputSql "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME NOT IN ('mysql','information_schema','performance_schema','sys','sysvarhub_db');" `
        -ReadOutput -FailureMessage "Nao foi possivel validar a exclusividade do MySQL do Hub.")
    if ($otherDatabases.Count -ne 0) {
        throw "MySQL contem outros bancos. Conta de backup nao provisionada; requer adequacao assistida."
    }
    # '_' e curinga em GRANT quando partial_revokes=OFF. Escopo literal em ambos os modos.
    $partialRevokes = @(Invoke-MySqlCommand -Arguments ((Get-MySqlAdminArgs) + @("--batch", "--skip-column-names")) `
        -InputSql "SELECT @@GLOBAL.partial_revokes;" -ReadOutput `
        -FailureMessage "Nao foi possivel validar o escopo literal dos privilegios de backup.")
    if ($partialRevokes.Count -ne 1 -or $partialRevokes[0] -notmatch '^[01]$') {
        throw "Configuracao de privilegios MySQL desconhecida; requer adequacao assistida."
    }
    $grantSchema = if ($partialRevokes[0] -eq '1') { '`sysvarhub_db`' } else { '`sysvarhub\_db`' }
    $backupPassword = New-Secret 48
    if ($backupPassword -notmatch '^[A-Za-z0-9_-]+$') { throw "Falha ao gerar credencial segura de backup." }
    # Arquivo vazio criado sem sobrescrita; ACL aplicada antes de gravar segredo.
    $file = [System.IO.File]::Open($CredentialFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $file.Dispose()
    Protect-SecretFile $CredentialFile
    @("[client]", "user=sysvarhub_backup", "password=$backupPassword", "host=127.0.0.1", "port=3307", "protocol=TCP") |
        Set-Content -LiteralPath $CredentialFile -Encoding ASCII
    # Sem IF NOT EXISTS: nunca assumir uma conta anterior nem ampliar seus acessos.
    Invoke-MySqlCommand -Arguments (Get-MySqlAdminArgs) `
        -InputSql "CREATE USER 'sysvarhub_backup'@'127.0.0.1' IDENTIFIED BY '$backupPassword'; GRANT SELECT, SHOW VIEW, TRIGGER ON $grantSchema.* TO 'sysvarhub_backup'@'127.0.0.1'; GRANT RELOAD, REPLICATION CLIENT, REPLICATION SLAVE ON *.* TO 'sysvarhub_backup'@'127.0.0.1';" `
        -FailureMessage "Provisionamento da conta de backup falhou. Preserve a credencial protegida e solicite adequacao assistida antes de tentar novamente."
    return $true
}


function Protect-SecretFile {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Caminho de backup/credencial nao pode ser um link." }
    $acl = if ($item.PSIsContainer) { New-Object System.Security.AccessControl.DirectorySecurity } else { New-Object System.Security.AccessControl.FileSecurity }
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = if ($item.PSIsContainer) { [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit' } else { [Security.AccessControl.InheritanceFlags]::None }
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
        $identity = New-Object Security.Principal.SecurityIdentifier($sid)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, [Security.AccessControl.FileSystemRights]::FullControl, $inheritance, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
}

function Assert-HubBackupLocalPath {
    param([string]$Path)
    if (-not [IO.Path]::IsPathRooted($Path) -or $Path.StartsWith('\\') -or $Path -match '[\r\n"]') { throw "Backup exige caminho local absoluto." }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $current = $full
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Backup nao permite links/juncoes nos caminhos." }
        }
        $parent = Split-Path -Path $current -Parent
        if ($parent -eq $current) { break }
        $current = $parent
    }
    return $full
}

function Assert-HubBackupCredential {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Credencial exclusiva de backup ausente; requer adequacao assistida." }
    $null = Assert-HubBackupLocalPath $Path
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) { throw "Credencial de backup sem ACL restrita." }
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin @('S-1-5-18','S-1-5-32-544')) { throw "Credencial de backup com acesso nao autorizado." }
    }
    $values = @{}
    $section = $false
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith('#') -or $line.StartsWith(';')) { continue }
        if ($line -eq '[client]' -and -not $section) { $section = $true; continue }
        if (-not $section -or $line -notmatch '^([a-z]+)=(.*)$' -or $values.ContainsKey($Matches[1])) { throw "Formato da credencial de backup invalido." }
        $values[$Matches[1]] = $Matches[2]
    }
    if ($values.Count -ne 5 -or $values.user -ne 'sysvarhub_backup' -or $values.host -ne '127.0.0.1' -or $values.port -ne '3307' -or $values.protocol -ne 'TCP' -or $values.password -notmatch '^[A-Za-z0-9_-]+$') { throw "Credencial deve pertencer exclusivamente a sysvarhub_backup em 127.0.0.1:3307." }
}

function Get-HubBackupConnectionArguments {
    param([string]$CredentialFile)
    return @("--defaults-file=$CredentialFile", '--host=127.0.0.1', '--port=3307', '--protocol=TCP', '--user=sysvarhub_backup', '--connect-timeout=5', '--default-character-set=utf8mb4')
}

function Get-HubMySqlDumpArguments {
    param([string]$CredentialFile, [string]$OutputFile)
    return @((Get-HubBackupConnectionArguments $CredentialFile) + @('--single-transaction','--quick','--source-data=2','--no-tablespaces','--set-gtid-purged=OFF','--hex-blob',"--result-file=$OutputFile",'--databases','sysvarhub_db'))
}

function ConvertTo-HubNativeArgument {
    param([string]$Value)
    # Quoting Win32 (CommandLineToArgvW/CRT); nenhum segredo e passado em argumentos.
    return '"' + ([regex]::Replace(([regex]::Replace($Value, '(\\*)"', '$1$1\"')), '(\\+)$', '$1$1')) + '"'
}

function Invoke-HubMySqlProcess {
    param([string]$Executable, [string[]]$Arguments, [string]$InputSql, [ValidateRange(1,86400)][int]$TimeoutSeconds = 30)
    $process = New-Object Diagnostics.Process
    $started = $false
    $process.StartInfo.FileName = $Executable
    $process.StartInfo.Arguments = (($Arguments | ForEach-Object { ConvertTo-HubNativeArgument $_ }) -join ' ')
    # Ignorar credenciais herdadas e o login-path pessoal do operador, sem alterar o ambiente pai.
    $process.StartInfo.EnvironmentVariables.Remove('MYSQL_PWD')
    $process.StartInfo.EnvironmentVariables['MYSQL_TEST_LOGIN_FILE'] = Join-Path ([IO.Path]::GetTempPath()) ('sysvarhub-no-login-' + [guid]::NewGuid().ToString('N'))
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardInput = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $process.StartInfo.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    try {
        if (-not $process.Start()) { throw 'start' }
        $started = $true
        try { $process.PriorityClass = [Diagnostics.ProcessPriorityClass]::BelowNormal } catch { }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if ($InputSql) { $process.StandardInput.WriteLine($InputSql) }
        $process.StandardInput.Close()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill()
            $process.WaitForExit()
            throw 'timeout'
        }
        $process.WaitForExit()
        $output = $stdout.GetAwaiter().GetResult()
        $null = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw 'exit' }
        return $output
    } catch {
        # Nunca repassar stderr/erro original: pode conter dados ou configuracao sensivel.
        throw "Ferramenta MySQL falhou ou excedeu o tempo limite. Verifique credencial, permissao e disponibilidade."
    } finally {
        if ($started -and -not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
        $process.Dispose()
    }
}

function Get-HubBackupQuery {
    param([string]$MysqlRoot, [string]$CredentialFile, [string]$Sql)
    return Invoke-HubMySqlProcess -Executable (Join-Path $MysqlRoot 'bin\mysql.exe') `
        -Arguments ((Get-HubBackupConnectionArguments $CredentialFile) + @('--batch','--raw','--skip-column-names','--database=sysvarhub_db')) -InputSql $Sql
}

function Assert-HubBackupGrants {
    param([string]$Grants, [bool]$PartialRevokes = $false)
    $globalPrivileges = @()
    $schemaPrivileges = @()
    foreach ($line in ($Grants -split '\r?\n' | Where-Object { $_.Trim() })) {
        if ($line -notmatch '^GRANT (.+) ON (.+) TO ' -or $line -match 'WITH GRANT OPTION') { throw "Conta de backup com permissoes nao homologadas." }
        $privileges = @($Matches[1] -split ', ')
        $rawScope = $Matches[2].Replace('`','')
        $scope = $rawScope.Replace('\_','_')
        if ($scope -eq '*.*') {
            foreach ($privilege in $privileges) {
                if ($privilege -notin @('USAGE','RELOAD','REPLICATION CLIENT','REPLICATION SLAVE')) { throw "Conta de backup com privilegios globais excessivos." }
            }
            $globalPrivileges += $privileges
        } elseif ($scope -eq 'sysvarhub_db.*') {
            if (-not $PartialRevokes -and $rawScope -ne 'sysvarhub\_db.*') { throw "Privilegio de backup contem curinga de banco nao permitido." }
            foreach ($privilege in $privileges) {
                if ($privilege -notin @('SELECT','SHOW VIEW','TRIGGER')) { throw "Conta de backup com privilegios de escrita nao homologados." }
            }
            $schemaPrivileges += $privileges
        } else { throw "Conta de backup tem acesso a outro banco." }
    }
    foreach ($privilege in @('RELOAD','REPLICATION CLIENT','REPLICATION SLAVE')) {
        if ($globalPrivileges -notcontains $privilege) { throw "Conta de backup sem privilegios globais necessarios." }
    }
    foreach ($privilege in @('SELECT','SHOW VIEW','TRIGGER')) {
        if ($schemaPrivileges -notcontains $privilege) { throw "Conta de backup sem privilegios de leitura necessarios." }
    }
}

function Get-HubBackupServerState {
    param([string]$MysqlRoot, [string]$ProgramDataRoot, [string]$CredentialFile)
    $grants = Get-HubBackupQuery $MysqlRoot $CredentialFile 'SHOW GRANTS FOR CURRENT_USER();'
    $sql = @"
SELECT JSON_OBJECT('partial_revokes',@@GLOBAL.partial_revokes,'version',VERSION(),'port',@@port,'basedir',@@basedir,'datadir',@@datadir,'log_bin',@@log_bin,'binlog_format',@@binlog_format,'server_uuid',@@server_uuid,'server_id',@@server_id,'account',CURRENT_USER(),'database',DATABASE(),'schemas',(SELECT JSON_ARRAYAGG(SCHEMA_NAME) FROM information_schema.SCHEMATA),'tables',(SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='sysvarhub_db' AND TABLE_TYPE='BASE TABLE'),'non_innodb',(SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='sysvarhub_db' AND TABLE_TYPE='BASE TABLE' AND (ENGINE IS NULL OR ENGINE<>'InnoDB')),'bytes',(SELECT COALESCE(SUM(DATA_LENGTH+INDEX_LENGTH),0) FROM information_schema.TABLES WHERE TABLE_SCHEMA='sysvarhub_db'),'routines',(SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='sysvarhub_db'),'events',(SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA='sysvarhub_db'));
"@
    try { $state = (Get-HubBackupQuery $MysqlRoot $CredentialFile $sql) | ConvertFrom-Json -ErrorAction Stop } catch { throw "Nao foi possivel validar metadados do MySQL do Hub." }
    if ($state.account -ne 'sysvarhub_backup@127.0.0.1' -or $state.database -ne 'sysvarhub_db' -or $state.port -ne 3307) { throw "Servidor/conta divergente: backup permitido somente na instancia oficial 3307." }
    Assert-HubBackupGrants $grants -PartialRevokes ([bool]$state.partial_revokes)
    $version = Get-HubMySqlBinaryVersion (Join-Path $MysqlRoot 'bin\mysqld.exe')
    if ($state.version -notmatch '^8\.0\.([0-9]+)(?:[-.]|$)' -or [int]$Matches[1] -ne $version.Build) { throw "Versao do servidor MySQL incompativel com as ferramentas." }
    if ([IO.Path]::GetFullPath($state.basedir).TrimEnd('\') -ine [IO.Path]::GetFullPath($MysqlRoot).TrimEnd('\') -or [IO.Path]::GetFullPath($state.datadir).TrimEnd('\') -ine [IO.Path]::GetFullPath((Join-Path $ProgramDataRoot 'mysql\data')).TrimEnd('\')) { throw "Diretorios MySQL divergentes da instalacao oficial do Hub." }
    if (@($state.schemas | Where-Object { $_ -notin @('mysql','information_schema','performance_schema','sys','sysvarhub_db') }).Count -ne 0 -or @($state.schemas) -notcontains 'sysvarhub_db') { throw "Instancia MySQL compartilhada ou banco incorreto; backup bloqueado." }
    if ($state.log_bin -ne 1 -or $state.binlog_format -ne 'ROW' -or $state.server_id -lt 1) { throw "Binlogs ROW indisponiveis para recuperacao incremental." }
    $master = (Get-HubBackupQuery $MysqlRoot $CredentialFile 'SHOW MASTER STATUS;').TrimEnd("`r","`n") -split "`t"
    if ($master.Count -lt 4 -or -not $master[0] -or [long]$master[1] -lt 4 -or $master[2] -or $master[3]) { throw "Coordenadas oficiais ausentes ou filtros de binlog nao homologados." }
    $serverGuid = [guid]::Empty
    if (-not [guid]::TryParse($state.server_uuid, [ref]$serverGuid) -or $serverGuid -eq [guid]::Empty) { throw "Identidade do servidor MySQL invalida." }
    if ($state.tables -lt 1 -or $state.non_innodb -ne 0 -or $state.routines -ne 0 -or $state.events -ne 0) { throw "Esquema nao homologado: exige tabelas InnoDB e ausencia de rotinas/eventos SQL." }
    try { $identities = @((Get-HubBackupQuery $MysqlRoot $CredentialFile 'SELECT JSON_OBJECT(''hub_uuid'',hub_uuid,''empresa_id'',empresa_id,''loja_id'',loja_id,''retaguarda_hub_id'',retaguarda_hub_id) FROM core_hubconfig;') -split '\r?\n' | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json -ErrorAction Stop }) } catch { throw "Identificacao da instalacao/loja indisponivel." }
    $hubGuid = [guid]::Empty
    if ($identities.Count -ne 1 -or -not [guid]::TryParse($identities[0].hub_uuid, [ref]$hubGuid) -or $hubGuid -eq [guid]::Empty -or $identities[0].empresa_id -lt 1 -or $identities[0].loja_id -lt 1) { throw "Backup exige uma unica instalacao com empresa e loja identificadas." }
    $state | Add-Member -NotePropertyName identity -NotePropertyValue $identities[0]
    return $state
}

function Assert-HubBackupService {
    param([string]$MysqlRoot, [string]$ProgramDataRoot)
    $service = Get-CimInstance Win32_Service -Filter "Name='SysvarHubMySQL'" -ErrorAction Stop
    $expectedExe = [regex]::Escape((Join-Path $MysqlRoot 'bin\mysqld.exe'))
    $expectedIni = [regex]::Escape((Join-Path $ProgramDataRoot 'config\my.ini'))
    if ($null -eq $service -or $service.Name -ne 'SysvarHubMySQL' -or $service.State -ne 'Running' -or $service.PathName -notmatch ('^"?' + $expectedExe + '"?\s') -or $service.PathName -notmatch ('--defaults-file="?' + $expectedIni + '"?(?:\s|$)')) { throw "Servico oficial SysvarHubMySQL ativo e dedicado nao encontrado. MySQL80/DEV nao e permitido." }
}

function Get-HubBackupCoordinates {
    param([string]$SqlPath)
    $reader = [IO.File]::OpenText($SqlPath)
    try {
        for ($i=0; $i -lt 2048 -and -not $reader.EndOfStream; $i++) {
            $line = $reader.ReadLine()
            if ($line -match "^-- CHANGE (?:REPLICATION SOURCE TO SOURCE|MASTER TO MASTER)_LOG_FILE='([A-Za-z0-9_.-]+\.[0-9]+)', (?:SOURCE|MASTER)_LOG_POS=([0-9]+);$") {
                $position = [long]$Matches[2]
                if ($position -lt 4) { break }
                return [pscustomobject]@{file=$Matches[1];position=$position}
            }
        }
    } finally { $reader.Dispose() }
    throw "Dump sem coordenadas validas de binlog no cabecalho."
}

function Assert-HubBackupCoordinates {
    param([string]$MysqlRoot, [string]$CredentialFile, $Coordinates)
    $logs = Get-HubBackupQuery $MysqlRoot $CredentialFile 'SHOW BINARY LOGS;'
    foreach ($line in ($logs -split '\r?\n')) {
        $parts = $line -split "`t"
        if ($parts.Count -ge 2 -and $parts[0] -eq $Coordinates.file -and [long]$parts[1] -ge $Coordinates.position) { return }
    }
    throw "Coordenadas do dump indisponiveis na cadeia atual de binlogs."
}

function Get-HubBackupFreeBytes {
    param([string]$BackupRoot)
    $drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($BackupRoot))
    if (-not $drive.IsReady -or $drive.DriveType -ne 'Fixed') { throw "Backup exige disco local fixo disponivel." }
    return $drive.AvailableFreeSpace
}

function Compress-HubBackupSql {
    param([string]$SqlPath, [string]$ArchivePath)
    $source = [IO.File]::OpenRead($SqlPath)
    $target = $null
    $gzip = $null
    try {
        $target = [IO.File]::Open($ArchivePath, [IO.FileMode]::CreateNew)
        $gzip = New-Object IO.Compression.GZipStream($target, [IO.Compression.CompressionLevel]::Fastest, $true)
        $source.CopyTo($gzip)
    } finally {
        if ($gzip) { $gzip.Dispose() }
        if ($target) { $target.Flush($true); $target.Dispose() }
        $source.Dispose()
    }
    $inputHash = (Get-FileHash -LiteralPath $SqlPath -Algorithm SHA256).Hash
    $compressed = [IO.File]::OpenRead($ArchivePath)
    $gzip = $null
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $gzip = New-Object IO.Compression.GZipStream($compressed, [IO.Compression.CompressionMode]::Decompress)
        $restoredHash = [BitConverter]::ToString($sha.ComputeHash($gzip)).Replace('-','')
        if ($restoredHash -ne $inputHash) { throw "Compactacao diverge do SQL original." }
    } finally {
        if ($gzip) { $gzip.Dispose() }
        $compressed.Dispose()
        $sha.Dispose()
    }
}

function Assert-HubBackupOperator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if ($identity.User.Value -ne 'S-1-5-18' -and -not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Execute o backup em Windows PowerShell como Administrador ou SYSTEM."
    }
}

function Invoke-HubMySqlFullBackup {
    param(
        [string]$InstallRoot = 'C:\Program Files\Sysvar Hub',
        [string]$ProgramDataRoot = 'C:\ProgramData\SysvarHub',
        [ValidateRange(1,86400)][int]$DumpTimeoutSeconds = 1200
    )
    $ErrorActionPreference = 'Stop'
    Assert-HubBackupOperator
    $InstallRoot = Assert-HubBackupLocalPath $InstallRoot
    $ProgramDataRoot = Assert-HubBackupLocalPath $ProgramDataRoot
    $mysqlRoot = Join-Path $InstallRoot 'mysql'
    $backupRoot = Assert-HubBackupLocalPath (Join-Path $ProgramDataRoot 'backup')
    $credential = Join-Path $ProgramDataRoot 'config\mysql-backup.cnf'
    Assert-HubBackupService $mysqlRoot $ProgramDataRoot
    $null = Assert-HubMySqlToolchain $mysqlRoot
    Assert-HubBackupCredential $credential
    $before = Get-HubBackupServerState $mysqlRoot $ProgramDataRoot $credential
    $requiredBytes = [Math]::Max(256MB, [double]$before.bytes * 4 + 64MB)
    if ((Get-HubBackupFreeBytes $backupRoot) -lt $requiredBytes) { throw "Espaco local insuficiente para dump e compactacao com margem de seguranca." }
    $null = New-Item -ItemType Directory -Path $backupRoot -Force
    Protect-SecretFile $backupRoot
    $lock = $null
    $staging = $null
    $createdStaging = $false
    $id = [guid]::NewGuid().ToString('N')
    $started = [DateTime]::UtcNow
    try {
        $lockPath = Assert-HubBackupLocalPath (Join-Path $backupRoot '.full-backup.lock')
        try { $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) } catch { throw "Outro backup completo esta em andamento ou o bloqueio local esta indisponivel." }
        $staging = Join-Path $backupRoot ('.incomplete-' + $id)
        if (Test-Path -LiteralPath $staging) { throw "Area temporaria ja existe; nada sera sobrescrito." }
        $null = New-Item -ItemType Directory -Path $staging
        $createdStaging = $true
        Protect-SecretFile $staging
        $sqlPath = Join-Path $staging 'sysvarhub_db.sql'
        $archivePath = Join-Path $staging 'sysvarhub_db.sql.gz'
        $null = Invoke-HubMySqlProcess -Executable (Join-Path $mysqlRoot 'bin\mysqldump.exe') -Arguments (Get-HubMySqlDumpArguments $credential $sqlPath) -TimeoutSeconds $DumpTimeoutSeconds
        if (-not (Test-Path -LiteralPath $sqlPath -PathType Leaf) -or (Get-Item -LiteralPath $sqlPath).Length -eq 0) { throw "mysqldump nao produziu SQL valido." }
        $coordinates = Get-HubBackupCoordinates $sqlPath
        Compress-HubBackupSql $sqlPath $archivePath
        $archive = Get-Item -LiteralPath $archivePath
        if ($archive.Length -le 0) { throw "Compactacao nao produziu arquivo valido." }
        $checksum = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($checksum -notmatch '^[0-9a-f]{64}$') { throw "Checksum do backup invalido." }
        Assert-HubBackupCoordinates $mysqlRoot $credential $coordinates
        $after = Get-HubBackupServerState $mysqlRoot $ProgramDataRoot $credential
        if ($before.server_uuid -ne $after.server_uuid -or ($before.identity | ConvertTo-Json -Compress) -ne ($after.identity | ConvertTo-Json -Compress)) { throw "Identidade MySQL/Hub mudou durante o backup; publicacao bloqueada." }
        $manifest = [ordered]@{
            manifest_version=1; backup_id=$id; type='FULL'; status='VALIDO'; started_at_utc=$started.ToString('o'); completed_at_utc=[DateTime]::UtcNow.ToString('o'); installation=$before.identity
            mysql=[ordered]@{version=$before.version; database='sysvarhub_db'; server_uuid=$before.server_uuid; server_id=$before.server_id; binlog_format=$before.binlog_format}
            binlog=[ordered]@{file=$coordinates.file; position=$coordinates.position; reference='SNAPSHOT_START'}
            archive=[ordered]@{name=$archive.Name; size_bytes=$archive.Length; sha256=$checksum; compression='gzip'; sql_size_bytes=(Get-Item -LiteralPath $sqlPath).Length}
        }
        $manifestPath = Join-Path $staging 'manifest.json'
        $stream = [IO.File]::Open($manifestPath, [IO.FileMode]::CreateNew)
        try {
            $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes(($manifest | ConvertTo-Json -Depth 6))
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        } finally { $stream.Dispose() }
        [IO.File]::Delete($sqlPath)
        $destination = Join-Path $backupRoot ('full-' + $started.ToString('yyyyMMddTHHmmssfffZ') + '-' + $id)
        # Rename no mesmo volume: pasta completa e manifesto aparecem juntos, sem overwrite.
        [IO.Directory]::Move($staging, $destination)
        $staging = $null
        return $destination
    } finally {
        try {
            if ($createdStaging -and $staging -and (Test-Path -LiteralPath $staging)) {
                # Resolver e limitar o alvo a area temporaria criada por esta execucao.
                $resolved = Assert-HubBackupLocalPath $staging
                if ((Split-Path $resolved -Parent) -ine $backupRoot -or (Split-Path $resolved -Leaf) -ne ('.incomplete-' + $id)) { throw 'Alvo temporario divergente.' }
                [IO.Directory]::Delete($resolved, $true)
            }
        } catch { Write-Warning "Area temporaria incompleta preservada e protegida para verificacao assistida." }
        finally { if ($lock) { $lock.Dispose() } }
    }
}
