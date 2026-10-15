$ErrorActionPreference='Stop'
$WindowsRoot=Split-Path $PSScriptRoot -Parent
. (Join-Path $WindowsRoot 'mysql-backup-control.ps1')
$script:realCompress=(Get-Command Compress-HubBackupSql).ScriptBlock
$script:realHead=(Get-Command Write-HubIncrementalHead).ScriptBlock

function New-FakeBinlog {
    param([string]$Path,[int]$Size=160,[int]$Seed=7)
    $data=New-Object byte[] $Size
    $data[0]=254; $data[1]=98; $data[2]=105; $data[3]=110
    $data[8]=15
    [BitConverter]::GetBytes([uint32]40).CopyTo($data,13)
    $data[21]=1
    $data[39]=1 # algoritmo CRC32 do FDE de 40 bytes
    for($i=44;$i -lt $Size;$i++) { $data[$i]=[byte](($i+$Seed)%251) }
    [IO.File]::WriteAllBytes($Path,$data)
}
function Add-FakeBinlogBytes {
    param([string]$Path,[int]$Count=20)
    $stream=[IO.File]::Open($Path,[IO.FileMode]::Append)
    try { $bytes=New-Object byte[] $Count; $stream.Write($bytes,0,$Count) } finally { $stream.Dispose() }
}

Describe 'Ancora de origem em binlog ficticio' {
    BeforeEach {
        $script:dataDir=Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $null=New-Item -ItemType Directory -Path $dataDir
        $script:logPath=Join-Path $dataDir 'sysvarhub-bin.000001'
        New-FakeBinlog $logPath
        $script:point=[pscustomobject]@{file='sysvarhub-bin.000001';position=80}
    }
    It 'detecta alteracao de bytes anteriores ao ponto confirmado' {
        $anchor=Get-HubBinlogPrefixAnchor $dataDir $point
        $bytes=[IO.File]::ReadAllBytes($logPath); $bytes[60]=99; [IO.File]::WriteAllBytes($logPath,$bytes)
        { Assert-HubBinlogSource $dataDir $point $anchor } | Should Throw 'alterado/recriado'
    }
    It 'permite append e fechamento normal sem invalidar o prefixo anterior' {
        $anchor=Get-HubBinlogPrefixAnchor $dataDir $point
        Add-FakeBinlogBytes $logPath
        $bytes=[IO.File]::ReadAllBytes($logPath); $bytes[21]=0; $bytes[40]=123; [IO.File]::WriteAllBytes($logPath,$bytes)
        { Assert-HubBinlogSource $dataDir $point $anchor } | Should Not Throw
    }
    It 'rejeita arquivo truncado, criptografado ou cabecalho invalido' {
        { Get-HubBinlogPrefixAnchor $dataDir ([pscustomobject]@{file=$point.file;position=999}) } | Should Throw 'menor'
        $bytes=[IO.File]::ReadAllBytes($logPath); $bytes[0]=253; [IO.File]::WriteAllBytes($logPath,$bytes)
        { Get-HubBinlogPrefixAnchor $dataDir $point } | Should Throw 'criptografado'
    }
}

Describe 'Comando local delimitado sem credencial administrativa' {
    It 'usa mysqlbinlog em uma chamada, filtro local e intervalo [inicio,fim)' {
        $start=[pscustomobject]@{file='sysvarhub-bin.000001';position=80}
        $window=[pscustomobject]@{end=[pscustomobject]@{file='sysvarhub-bin.000002';position=120};files=@([pscustomobject]@{file=$start.file},[pscustomobject]@{file='sysvarhub-bin.000002'})}
        $args=Get-HubMysqlbinlogArguments 'C:\snapshot protegido' 'C:\snapshot protegido\segment.sql' $start $window
        $args[0] | Should Be '--no-defaults'
        foreach($flag in @('--verify-binlog-checksum','--force-if-open','--database=sysvarhub_db','--base64-output=AUTO','--start-position=80','--stop-position=120')) { ($args -contains $flag) | Should Be $true }
        ($args -join ' ') | Should Not Match 'root|password|read-from-remote|stop-never|raw|3306|force-read|short-form'
        $args[-2] | Should Be 'C:\snapshot protegido\sysvarhub-bin.000001'
        $args[-1] | Should Be 'C:\snapshot protegido\sysvarhub-bin.000002'
    }
    It 'identifica saida somente de metadados sem publicar alteracoes ficticias' {
        $file=Join-Path $TestDrive 'metadata.sql'
        Set-Content $file '# Start: binlog v 4; Rotate to sysvarhub-bin.000002'
        Test-HubBinlogSqlChanges $file | Should Be $false
        Add-Content $file '# server id 1 end_log_pos 100 Write_rows: table id 1'
        Test-HubBinlogSqlChanges $file | Should Be $true
    }
}

Describe 'Captura incremental isolada com FULL ficticio' {
    BeforeEach {
        $script:caseRoot=Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $script:installRoot=Join-Path $caseRoot 'app'; $script:dataRoot=Join-Path $caseRoot 'state'; $script:backupRoot=Join-Path $dataRoot 'backup'; $script:mysqlData=Join-Path $dataRoot 'mysql\data'
        $null=New-Item -ItemType Directory -Path $backupRoot,$mysqlData -Force
        $script:source=Join-Path $mysqlData 'sysvarhub-bin.000001'
        New-FakeBinlog $source
        $script:fullId='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        $script:fullDir=Join-Path $backupRoot ('full-20261015T120000000Z-' + $fullId)
        $null=New-Item -ItemType Directory -Path $fullDir
        $script:identity=[pscustomobject]@{hub_uuid='22222222222242228222222222222222';empresa_id=1;loja_id=10;retaguarda_hub_id=20}
        $script:state=[pscustomobject]@{version='8.0.46';server_uuid='11111111-1111-4111-8111-111111111111';server_id=1;datadir=$mysqlData;binlog_checksum='CRC32';binlog_encryption=0;identity=$identity}
        $point=[pscustomobject]@{file='sysvarhub-bin.000001';position=80}
        $anchor=Get-HubBinlogPrefixAnchor $mysqlData $point
        $sql=Join-Path $caseRoot 'full-ficticio.sql'; Set-Content $sql '-- SQL inteiramente ficticio'
        $archive=Join-Path $fullDir 'sysvarhub_db.sql.gz'
        & $script:realCompress $sql $archive
        $script:fullManifest=[ordered]@{manifest_version=1;type='FULL';status='VALIDO';backup_id=$fullId;completed_at_utc=[DateTime]::UtcNow.ToString('o');installation=$identity;mysql=[ordered]@{database='sysvarhub_db';server_uuid=$state.server_uuid};binlog=[ordered]@{file=$point.file;position=$point.position;anchor=$anchor};archive=[ordered]@{name='sysvarhub_db.sql.gz';size_bytes=(Get-Item $archive).Length;sha256=(Get-FileHash $archive).Hash.ToLowerInvariant();compression='gzip'}}
        Write-HubBackupJson (Join-Path $fullDir 'manifest.json') $fullManifest
        $script:fullHash=(Get-FileHash (Join-Path $fullDir 'manifest.json')).Hash
        $script:fullArchiveHash=(Get-FileHash $archive).Hash
        $script:masterFile='sysvarhub-bin.000001'; $script:masterPosition=160
        $script:failCapture=$false; $script:failCompression=$false; $script:failHead=$false; $script:metadataOnly=$false; $script:changeUuidDuringCapture=$false
        Mock Assert-HubBackupOperator { }
        Mock Assert-HubBackupService { }
        Mock Assert-HubMySqlToolchain { Join-Path $script:installRoot 'mysql' }
        Mock Assert-HubBackupCredential { }
        Mock Protect-SecretFile { }
        Mock Get-HubBackupServerState { $script:state | Select-Object * }
        Mock Get-HubBackupFreeBytes { 10GB }
        Mock Get-HubBackupQuery {
            if ($Sql -eq 'SHOW MASTER STATUS;') { return "$($script:masterFile)`t$($script:masterPosition)`t`t`t" }
            if ($Sql -eq 'SHOW BINARY LOGS;') { return ((Get-ChildItem -LiteralPath $script:mysqlData -File | Sort-Object Name | ForEach-Object { "$($_.Name)`t$($_.Length)`tNo" }) -join "`n") }
            throw 'Consulta nao prevista no teste'
        }
        Mock Invoke-HubMySqlProcess {
            (Get-Item -LiteralPath $Arguments[-1]).Length | Should Be $script:masterPosition
            $path=($Arguments | Where-Object { $_ -like '--result-file=*' }).Substring('--result-file='.Length)
            Set-Content -LiteralPath $path -Value '# Start: binlog v4'
            if($script:failCapture) { throw 'Conexao interrompida ficticia' }
            if(-not $script:metadataOnly) { Add-Content -LiteralPath $path -Value "# server id 1 end_log_pos $($script:masterPosition) Write_rows: table id 1" }
            if($script:changeUuidDuringCapture) { $script:state.server_uuid="33333333-3333-4333-8333-333333333333" }
        }
        Mock Compress-HubBackupSql {
            if($script:failCompression) { Set-Content $ArchivePath 'parcial'; throw 'Compactacao ficticia falhou' }
            & $script:realCompress $SqlPath $ArchivePath
        }
        Mock Write-HubIncrementalHead {
            if($script:failHead) { throw 'Interrupcao ficticia apos publicacao' }
            & $script:realHead $BackupRoot $Chain
        }
    }
    AfterEach {
        (Get-FileHash (Join-Path $fullDir 'sysvarhub_db.sql.gz')).Hash | Should Be $fullArchiveHash
        @(Get-ChildItem -LiteralPath $backupRoot -Directory -Force | Where-Object { $_.Name -like '.incomplete-*' }).Count | Should Be 0
    }
    It 'publica primeiro incremental a partir da coordenada FULL e registra ultimo ponto' {
        $published=Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot
        $m=Read-HubBackupJson (Join-Path $published 'manifest.json')
        $m.sequence | Should Be 1; $m.start.position | Should Be 80; $m.end.position | Should Be 160
        $m.previous_id | Should Be $fullId; $m.full_backup_id | Should Be $fullId; $m.status | Should Be 'VALIDO'
        $m.archive.sha256 | Should Be (Get-FileHash (Join-Path $published $m.archive.name)).Hash.ToLowerInvariant()
        @(Get-ChildItem -LiteralPath $published -File).Count | Should Be 2
        $head=Read-HubBackupJson (Join-Path $backupRoot ('incremental-head-' + $fullId + '.json'))
        $head.point.position | Should Be 160; $head.last_id | Should Be $m.incremental_id
        (Get-FileHash (Join-Path $fullDir 'manifest.json')).Hash | Should Be $fullHash
    }
    It 'inicia segundo incremental no fim exclusivo do primeiro sem sobreposicao' {
        $one=Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot
        $oldHash=(Get-FileHash (Join-Path $one 'manifest.json')).Hash
        Add-FakeBinlogBytes $source; $script:masterPosition=180
        $two=Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot
        $m=Read-HubBackupJson (Join-Path $two 'manifest.json')
        $m.sequence | Should Be 2; $m.start.position | Should Be 160; $m.end.position | Should Be 180
        $m.previous_manifest_sha256 | Should Be $oldHash.ToLowerInvariant()
        (Get-FileHash (Join-Path $one 'manifest.json')).Hash | Should Be $oldHash
    }
    It 'nao publica nem chama mysqlbinlog sem mudanca da coordenada' {
        $script:masterPosition=80
        @(Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot).Count | Should Be 0
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 0 -Exactly
    }
    It 'nao publica segmento com apenas metadados de rotacao' {
        $script:metadataOnly=$true
        @(Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot).Count | Should Be 0
        @(Get-ChildItem -LiteralPath $backupRoot -Directory).Count | Should Be 1
    }
    It 'trata rotacao em sequencia exata e conserva posicoes originais' {
        New-FakeBinlog (Join-Path $mysqlData 'sysvarhub-bin.000002') 120
        $script:masterFile='sysvarhub-bin.000002'; $script:masterPosition=120
        $dir=Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot
        $m=Read-HubBackupJson (Join-Path $dir 'manifest.json')
        $m.start.file | Should Be 'sysvarhub-bin.000001'; $m.end.file | Should Be 'sysvarhub-bin.000002'
        $m.sources.Count | Should Be 2; $m.sources[0].position | Should Be 160; $m.sources[1].position | Should Be 120
    }
    It 'bloqueia lacuna entre arquivos rotacionados' {
        New-FakeBinlog (Join-Path $mysqlData 'sysvarhub-bin.000003') 120
        $script:masterFile='sysvarhub-bin.000003'; $script:masterPosition=120
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'Lacuna'
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 0 -Exactly
    }
    It 'bloqueia origem ausente ou expirada' {
        [IO.File]::Delete($source)
        New-FakeBinlog (Join-Path $mysqlData 'sysvarhub-bin.000002') 120
        $script:masterFile='sysvarhub-bin.000002'; $script:masterPosition=120
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'expirado'
    }
    It 'bloqueia binlog recriado com mesmo nome e UUID' {
        New-FakeBinlog $source 160 8
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'alterado/recriado'
    }
    It 'bloqueia mudanca do UUID do servidor' {
        $state.server_uuid='33333333-3333-4333-8333-333333333333'
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'UUID'
    }
    It 'nao publica se o UUID mudar durante a captura' {
        $script:changeUuidDuringCapture=$true
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'UUID/identidade'
        @(Get-ChildItem -LiteralPath $backupRoot -Directory).Count | Should Be 1
    }
    It 'bloqueia FULL antigo sem ancora e exige nova base segura' {
        $fullManifest.binlog.Remove('anchor')
        [IO.File]::Delete((Join-Path $fullDir 'manifest.json')); Write-HubBackupJson (Join-Path $fullDir 'manifest.json') $fullManifest
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'novo FULL'
    }
    It 'bloqueia manifesto JSON invalido sem expor seu conteudo' {
        Set-Content -LiteralPath (Join-Path $fullDir 'manifest.json') -Value 'nao e JSON'
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'JSON invalido'
    }
    It 'trata falha de conexao sem publicar parcial' {
        Mock Get-HubBackupServerState { throw 'Conexao ficticia falhou' }
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'Conexao'
        @(Get-ChildItem -LiteralPath $backupRoot -Directory).Count | Should Be 1
    }
    It 'trata compactacao interrompida preservando cadeia anterior' {
        $script:failCompression=$true
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'Compactacao'
        @(Get-ChildItem -LiteralPath $backupRoot -Directory).Count | Should Be 1
        Test-Path (Join-Path $backupRoot ('incremental-head-' + $fullId + '.json')) | Should Be $false
    }
    It 'retoma apos interrupcao sem duplicar o incremental anterior' {
        $one=Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot
        $hash=(Get-FileHash (Join-Path $one 'manifest.json')).Hash
        Add-FakeBinlogBytes $source; $script:masterPosition=180; $script:failCapture=$true
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'interrompida'
        $script:failCapture=$false
        $two=Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot
        (Read-HubBackupJson (Join-Path $two 'manifest.json')).sequence | Should Be 2
        (Get-FileHash (Join-Path $one 'manifest.json')).Hash | Should Be $hash
    }
    It 'recupera checkpoint interrompido depois da publicacao sem nova captura' {
        $script:failHead=$true
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'apos publicacao'
        $script:failHead=$false
        @(Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot).Count | Should Be 0
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 1 -Exactly
        $head=Read-HubBackupJson (Join-Path $backupRoot ('incremental-head-' + $fullId + '.json'))
        $head.sequence | Should Be 1
    }
    It 'bloqueia alteracao de manifesto incremental previamente confirmado' {
        $one=Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot
        Add-Content -LiteralPath (Join-Path $one 'manifest.json') -Value ' '
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'confirmado'
    }
    It 'bloqueia execucao simultanea com o FULL' {
        $lock=Enter-HubBackupLock $backupRoot
        try { { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'bloqueio local' } finally { $lock.Dispose() }
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 0 -Exactly
    }
    It 'delimita prefixo ativo sem copiar bytes posteriores ao ponto oficial' {
        $script:masterPosition=100
        $dir=Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot
        $m=Read-HubBackupJson (Join-Path $dir 'manifest.json')
        $m.end.position | Should Be 100
        (Get-Item -LiteralPath $source).Length | Should Be 160
    }
    It 'detecta remocao do ultimo incremental pelo checkpoint confirmado' {
        $one=Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot
        $resolved=[IO.Path]::GetFullPath($one)
        if(-not $resolved.StartsWith([IO.Path]::GetFullPath($caseRoot),[StringComparison]::OrdinalIgnoreCase)) { throw 'Fixture fora do diretorio de teste' }
        [IO.Directory]::Move($resolved,(Join-Path $caseRoot 'quarentena-ficticia'))
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'confirmado'
    }
    It 'detecta arquivo incremental corrompido sem avancar a cadeia' {
        $one=Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot
        Add-Content -LiteralPath (Join-Path $one 'binlog-segment.sql.gz') -Value 'corrupcao ficticia'
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'checksum/tamanho'
    }
    It 'controlador reutiliza captura e checkpoint reais em arquivos ficticios sem duplicar no retry' {
        $first=Invoke-HubBackupControl $installRoot $dataRoot
        $first.status | Should Be 'INCREMENTAL_VALIDO'
        $first.last_incremental.sequence | Should Be 1
        $first.published_bytes | Should BeGreaterThan 0
        $second=Invoke-HubBackupControl $installRoot $dataRoot
        $second.status | Should Be 'SEM_ALTERACOES'
        $second.last_incremental.sequence | Should Be 1
        @(Get-ChildItem $backupRoot -Directory -Filter 'incremental-*').Count | Should Be 1
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 1 -Exactly
    }
    It 'controlador repara checkpoint apos interrupcao sem recapturar nem reiniciar cadeia' {
        $script:failHead=$true
        (Invoke-HubBackupControl $installRoot $dataRoot).status | Should Be 'FALHA_REPETIR'
        $script:failHead=$false
        $result=Invoke-HubBackupControl $installRoot $dataRoot
        $result.status | Should Be 'SEM_ALTERACOES'
        $result.last_incremental.sequence | Should Be 1
        $head=Read-HubBackupJson (Join-Path $backupRoot ('incremental-head-' + $fullId + '.json'))
        $head.sequence | Should Be 1
        @(Get-ChildItem $backupRoot -Directory -Filter 'incremental-*').Count | Should Be 1
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 1 -Exactly
    }
    It 'controlador descobre cadeia incremental existente na primeira tentativa' {
        $null=Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot
        $result=Invoke-HubBackupControl $installRoot $dataRoot
        $result.status | Should Be 'SEM_ALTERACOES'
        $result.last_incremental.sequence | Should Be 1
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 1 -Exactly
    }
    It 'controlador bloqueia incremental existente corrompido' {
        $one=Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot
        Add-Content -LiteralPath (Join-Path $one 'binlog-segment.sql.gz') -Value 'corrupcao ficticia'
        (Invoke-HubBackupControl $installRoot $dataRoot).support_required | Should Be $true
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 1 -Exactly
    }
    It 'bloqueia MySQL DEV antes de qualquer captura' {
        Mock Assert-HubBackupService { throw 'MySQL80/DEV nao permitido' }
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'DEV'
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 0 -Exactly
    }
    It 'bloqueia falta de espaco sem avancar ultimo ponto' {
        Mock Get-HubBackupFreeBytes { 1 }
        { Invoke-HubMySqlIncrementalBackup $fullDir $installRoot $dataRoot } | Should Throw 'Espaco'
        Assert-MockCalled Invoke-HubMySqlProcess -Scope It -Times 0 -Exactly
    }
    It 'renova FULL apos captura real ficticia mantendo nova ancora e cadeia anterior' {
        Mock Test-HubFullRenewalWindow { $true }
        Mock Invoke-HubMySqlFullBackup {
            $name='full-20261016T120000000Z-dddddddddddddddddddddddddddddddd'
            $dir=Join-Path $script:backupRoot $name; $null=New-Item -ItemType Directory -Path $dir
            Copy-Item -LiteralPath (Join-Path $script:fullDir 'sysvarhub_db.sql.gz') -Destination $dir
            $manifest=Read-HubBackupJson (Join-Path $script:fullDir 'manifest.json')
            $manifest.backup_id='dddddddddddddddddddddddddddddddd'
            $manifest.completed_at_utc=[DateTime]::UtcNow.ToString('o')
            $manifest.binlog.position=$script:masterPosition
            $manifest.binlog.anchor=Get-HubBinlogPrefixAnchor $script:mysqlData $manifest.binlog
            Write-HubBackupJson (Join-Path $dir 'manifest.json') $manifest
            return $dir
        }
        $result=Invoke-HubBackupControl $installRoot $dataRoot
        $result.status | Should Be 'FULL_RENOVADO'
        $result.active_full | Should Match 'dddddddd'
        $result.last_incremental.full_backup_id | Should Be $fullId
        @(Get-ChildItem $backupRoot -Directory -Filter 'incremental-*').Count | Should Be 1
        Test-Path $fullDir | Should Be $true
    }
}


Describe 'Sintaxe do acionamento incremental em Windows PowerShell 5.1' {
    It 'valida os scripts sem executar captura ou instaladores' {
        foreach($name in @('mysql-backup.ps1','backup-full.ps1','backup-incremental.ps1')) {
            $parseErrors=$null
            $null=[Management.Automation.Language.Parser]::ParseFile((Join-Path $WindowsRoot $name),[ref]$null,[ref]$parseErrors)
            @($parseErrors).Count | Should Be 0
        }
    }
}
