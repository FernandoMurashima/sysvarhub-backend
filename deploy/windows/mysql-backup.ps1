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
