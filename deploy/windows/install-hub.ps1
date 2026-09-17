param(
    [string]$InstallRoot = "C:\Program Files\Sysvar Hub",
    [string]$ProgramDataRoot = "C:\ProgramData\SysvarHub"
)

$ErrorActionPreference = "Stop"

$ConfigRoot = Join-Path $ProgramDataRoot "config"
$LogRoot = Join-Path $ProgramDataRoot "logs"
$MysqlData = Join-Path $ProgramDataRoot "mysql\data"
$EnvFile = Join-Path $ConfigRoot "sysvarhub.env"
$MysqlAdminFile = Join-Path $ConfigRoot "mysql-admin.cnf"
$MyIni = Join-Path $ConfigRoot "my.ini"
$ServiceExe = Join-Path $InstallRoot "runtime\SysvarHubService.exe"
$Mysqld = Join-Path $InstallRoot "mysql\bin\mysqld.exe"
$Mysql = Join-Path $InstallRoot "mysql\bin\mysql.exe"
$AclSystem = "*S-1-5-18:F"
$AclAdministrators = "*S-1-5-32-544:F"

New-Item -ItemType Directory -Force -Path $ConfigRoot, $LogRoot, (Join-Path $ProgramDataRoot "backup"), (Join-Path $ProgramDataRoot "data"), $MysqlData | Out-Null

function New-Secret([int]$Length = 48) {
    $bytes = New-Object byte[] $Length
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

if (-not (Test-Path $EnvFile)) {
    $SecretKey = New-Secret 64
    $DbPassword = New-Secret 48
    @(
        "DJANGO_SECRET_KEY=$SecretKey",
        "DJANGO_DEBUG=False",
        "DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost",
        "DJANGO_CSRF_TRUSTED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000",
        "CORS_ALLOWED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000",
        "DB_NAME=sysvarhub_db",
        "DB_USER=sysvarhub",
        "DB_PASSWORD=$DbPassword",
        "DB_HOST=127.0.0.1",
        "DB_PORT=3307",
        "HUB_BIND_HOST=0.0.0.0",
        "HUB_PORT=8000",
        "SYSVARHUB_LOG_DIR=$LogRoot",
        "SYSVARHUB_FRONTEND_DIST_DIR=$(Join-Path $InstallRoot 'frontend')"
    ) | Set-Content -LiteralPath $EnvFile -Encoding UTF8
    icacls $EnvFile /inheritance:r | Out-Null
    icacls $EnvFile /grant:r $AclSystem $AclAdministrators | Out-Null
}

if (-not (Test-Path $MyIni)) {
    @(
        "[mysqld]",
        "basedir=$($InstallRoot.Replace('\','/'))/mysql",
        "datadir=$($MysqlData.Replace('\','/'))",
        "port=3307",
        "bind-address=127.0.0.1",
        "character-set-server=utf8mb4",
        "collation-server=utf8mb4_unicode_ci",
        "default-storage-engine=INNODB",
        "innodb_flush_log_at_trx_commit=1",
        "[client]",
        "default-character-set=utf8mb4"
    ) | Set-Content -LiteralPath $MyIni -Encoding ASCII
}

if (-not (Test-Path (Join-Path $MysqlData "mysql"))) {
    $AdminPassword = New-Secret 48
    & $Mysqld --defaults-file="$MyIni" --initialize-insecure
    if ($LASTEXITCODE -ne 0) { throw "Inicializacao do MySQL falhou." }
    $FirstRun = $true
}

if (-not (Get-Service -Name "SysvarHubMySQL" -ErrorAction SilentlyContinue)) {
    & $Mysqld --install SysvarHubMySQL --defaults-file="$MyIni"
    if ($LASTEXITCODE -ne 0) { throw "Registro do servico SysvarHubMySQL falhou." }
    sc.exe config SysvarHubMySQL start= auto | Out-Null
}

Start-Service SysvarHubMySQL
Start-Sleep -Seconds 5

$Env = Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match "=" } | ForEach-Object {
    $parts = $_.Split("=", 2); @{ $parts[0] = $parts[1] }
}
$DbPasswordLine = Get-Content -LiteralPath $EnvFile | Where-Object { $_.StartsWith("DB_PASSWORD=") } | Select-Object -First 1
$DbPassword = $DbPasswordLine.Split("=", 2)[1]

if ($FirstRun) {
    & $Mysql -h127.0.0.1 -P3307 -uroot -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '$AdminPassword'; FLUSH PRIVILEGES;"
    if ($LASTEXITCODE -ne 0) { throw "Protecao do usuario administrativo MySQL falhou." }
    @("[client]", "user=root", "password=$AdminPassword", "host=127.0.0.1", "port=3307") | Set-Content -LiteralPath $MysqlAdminFile -Encoding ASCII
    icacls $MysqlAdminFile /inheritance:r | Out-Null
    icacls $MysqlAdminFile /grant:r $AclSystem $AclAdministrators | Out-Null
}
if (-not (Test-Path $MysqlAdminFile)) {
    throw "Credencial administrativa local do MySQL nao encontrada em $MysqlAdminFile."
}

& $Mysql --defaults-extra-file="$MysqlAdminFile" -e "CREATE DATABASE IF NOT EXISTS sysvarhub_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER IF NOT EXISTS 'sysvarhub'@'127.0.0.1' IDENTIFIED BY '$DbPassword'; GRANT ALL PRIVILEGES ON sysvarhub_db.* TO 'sysvarhub'@'127.0.0.1'; FLUSH PRIVILEGES;"
if ($LASTEXITCODE -ne 0) { throw "Preparacao do banco Sysvar Hub falhou." }

& $ServiceExe manage migrate --noinput
if ($LASTEXITCODE -ne 0) { throw "Migrations falharam." }
& $ServiceExe manage preparar_instalacao
if ($LASTEXITCODE -ne 0) { throw "Preparacao da instalacao falhou." }

if (-not (Get-Service -Name "SysvarHub" -ErrorAction SilentlyContinue)) {
    & $ServiceExe install --startup auto
    if ($LASTEXITCODE -ne 0) { throw "Registro do servico SysvarHub falhou." }
}
sc.exe config SysvarHub depend= SysvarHubMySQL start= delayed-auto | Out-Null

New-NetFirewallRule -DisplayName "Sysvar Hub" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Private,Domain -RemoteAddress LocalSubnet -ErrorAction SilentlyContinue | Out-Null

Start-Service SysvarHub
& (Join-Path $PSScriptRoot "check-hub.ps1") -WaitSeconds 60 -IntervalSeconds 2
