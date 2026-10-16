# Fixture integrada: geradores reais, somente fronteira MySQL/Windows simulada.
$ErrorActionPreference='Stop'
$WindowsRoot=Split-Path $PSScriptRoot -Parent
. (Join-Path $WindowsRoot 'mysql-backup.ps1')
$ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'mysql-incremental-backup.Tests.ps1'),[ref]$null,[ref]$null)
$node=$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'New-FakeBinlog'},$false)[0]
. ([scriptblock]::Create($node.Extent.Text))

Describe 'Fixture integrada FULL e dois incrementais reais com ferramentas simuladas' {
    It 'exporta cadeia ficticia para o teste HTTPS da Central sem MySQL ou servicos' {
        if ($env:SYSVAR_BACKUP_TEST_FIXTURE_ROOT) {
            $script:fixtureRoot=Assert-HubBackupLocalPath $env:SYSVAR_BACKUP_TEST_FIXTURE_ROOT
            if ((Split-Path $fixtureRoot -Parent) -ine [IO.Path]::GetTempPath().TrimEnd('\') -or (Split-Path $fixtureRoot -Leaf) -notlike 'sysvar-backup-e2e-case-*') { throw 'Exportacao somente em pasta temporaria exclusiva de teste.' }
            $script:scenario=Read-HubBackupJson (Join-Path $fixtureRoot 'identity.json')
            if ($scenario.test_only -ne 'FICTITIOUS_SYSVAR_BACKUP_INTEGRATION') { throw 'Identidade nao identificada como ficticia.' }
        } else {
            $script:fixtureRoot=Join-Path $TestDrive 'fixture'
            $null=New-Item -ItemType Directory -Path $fixtureRoot
            $script:scenario=[pscustomobject]@{test_only='FICTITIOUS_SYSVAR_BACKUP_INTEGRATION';identity=[pscustomobject]@{hub_uuid=[guid]::NewGuid().ToString();empresa_id=1;loja_id=2;retaguarda_hub_id=3};server_uuid=[guid]::NewGuid().ToString();payload_bytes=1024;evento=[pscustomobject]@{evento_uuid=[guid]::NewGuid().ToString();chave_idempotencia='fixture-only';tipo='VENDA_FINALIZADA';payload=[pscustomobject]@{venda_uuid=[guid]::NewGuid().ToString()}}}
        }
        $script:installRoot=Join-Path $fixtureRoot 'install'
        $script:dataRoot=Join-Path $fixtureRoot 'state'
        $script:backupRoot=Join-Path $dataRoot 'backup'
        $script:mysqlData=Join-Path $dataRoot 'mysql\data'
        $null=New-Item -ItemType Directory -Path $backupRoot,$mysqlData -Force
        $script:logPath=Join-Path $mysqlData 'sysvarhub-bin.000001'
        New-FakeBinlog $logPath -Size 1024
        $script:endPosition=157
        $script:server=[pscustomobject]@{version='8.0.46';server_uuid=$scenario.server_uuid;server_id=1;datadir=$mysqlData;binlog_format='ROW';binlog_checksum='CRC32';identity=$scenario.identity;bytes=1024}
        Mock Assert-HubBackupOperator {}
        Mock Assert-HubBackupService {}
        Mock Assert-HubMySqlToolchain { Join-Path $script:installRoot 'mysql' }
        Mock Assert-HubBackupCredential {}
        Mock Protect-SecretFile {}
        Mock Get-HubBackupServerState { $script:server | Select-Object * }
        Mock Get-HubBackupFreeBytes { 10GB }
        Mock Start-Service { throw 'Servico real proibido.' }
        Mock Stop-Service { throw 'Servico real proibido.' }
        Mock Get-HubBackupQuery {
            if ($Sql -eq 'SHOW MASTER STATUS;') { return "sysvarhub-bin.000001`t$script:endPosition`t`t`t" }
            if ($Sql -eq 'SHOW BINARY LOGS;') { return "sysvarhub-bin.000001`t1024`tNo" }
            throw 'Consulta MySQL nao prevista no teste.'
        }
        Mock Invoke-HubMySqlProcess {
            $path=($Arguments | Where-Object { $_ -like '--result-file=*' }).Substring('--result-file='.Length)
            if ($Executable.EndsWith('mysqldump.exe')) {
                $bytes=New-Object byte[] $scenario.payload_bytes
                (New-Object Random(42)).NextBytes($bytes)
                $text="-- Server version`t8.0.46`n-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='sysvarhub-bin.000001', SOURCE_LOG_POS=157;`n-- FULL_FICTITIOUS`n-- EVENT="+($scenario.evento | ConvertTo-Json -Depth 8 -Compress)+"`n-- "+[Convert]::ToBase64String($bytes)+"`n"
            } elseif ($Executable.EndsWith('mysqlbinlog.exe')) {
                $text="# server id 1 end_log_pos $script:endPosition Write_rows: table id 1`n-- INCREMENTAL_FICTITIOUS_$script:endPosition`n-- EVENT="+($scenario.evento | ConvertTo-Json -Depth 8 -Compress)+"`n"
            } else { throw 'Executavel real ou inesperado bloqueado pelo fixture.' }
            [IO.File]::WriteAllText($path,$text,(New-Object Text.UTF8Encoding($false)))
        }
        $full=Invoke-HubMySqlFullBackup $installRoot $dataRoot
        $original=(Get-FileHash (Join-Path $full 'sysvarhub_db.sql.gz')).Hash
        $script:endPosition=333
        $one=Invoke-HubMySqlIncrementalBackup $full $installRoot $dataRoot
        $script:endPosition=555
        $two=Invoke-HubMySqlIncrementalBackup $full $installRoot $dataRoot
        @(Invoke-HubMySqlIncrementalBackup $full $installRoot $dataRoot).Count | Should Be 0
        $chain=Get-HubIncrementalChain $backupRoot $full $server
        $chain.sequence | Should Be 2
        $chain.point.position | Should Be 555
        (Get-FileHash (Join-Path $full 'sysvarhub_db.sql.gz')).Hash | Should Be $original
        $directories=@($full,$one,$two | ForEach-Object { Split-Path $_ -Leaf })
        $plan=[ordered]@{version=1;service='SysvarHubMySQL';port=3307;server_uuid=$scenario.server_uuid;backup_root=$backupRoot;directories=$directories;policy=[ordered]@{version=1;enabled=$true;bandwidth_bytes_per_second=16777216;cycle_seconds=120;request_timeout_seconds=5;finalize_timeout_seconds=30}}
        Write-HubBackupJson (Join-Path $fixtureRoot 'fixture-plan.json') $plan
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 3 -Exactly
        Assert-MockCalled Start-Service -Scope It -Times 0 -Exactly
        Assert-MockCalled Stop-Service -Scope It -Times 0 -Exactly
    }
}
