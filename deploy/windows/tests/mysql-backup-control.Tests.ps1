$ErrorActionPreference='Stop'
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'mysql-backup-control.ps1')

function New-ControlTestFull {
    param([string]$Name,[int]$AgeDays=1)
    $dir=Join-Path $script:backupRoot $Name
    $null=New-Item -ItemType Directory -Path $dir
    $archive=Join-Path $dir 'sysvarhub_db.sql.gz'
    Set-Content -LiteralPath $archive -Value 'arquivo ficticio, sem dados operacionais'
    $point=[ordered]@{file='sysvarhub-bin.000001';position=80;anchor=[ordered]@{algorithm='MYSQL8_CRC32_PREFIX_V1';file='sysvarhub-bin.000001';position=80;sha256=('b'*64)}}
    $manifest=[ordered]@{manifest_version=1;type='FULL';status='VALIDO';backup_id=$Name.Substring($Name.Length-32);completed_at_utc=[DateTime]::UtcNow.AddDays(-$AgeDays).ToString('o');installation=$script:identity;mysql=[ordered]@{database='sysvarhub_db';server_uuid=$script:server.server_uuid};binlog=$point;archive=[ordered]@{name='sysvarhub_db.sql.gz';size_bytes=(Get-Item $archive).Length;sha256=(Get-FileHash $archive).Hash.ToLowerInvariant();compression='gzip'}}
    Write-HubBackupJson (Join-Path $dir 'manifest.json') $manifest
    return $dir
}

Describe 'Politica semanal e janela local' {
    It 'usa sete dias e janela de menor movimento por padrao' {
        $policy=Get-HubBackupPolicy $TestDrive
        $policy.renewal_days | Should Be 7
        $policy.window_start | Should Be '03:00'
        $policy.window_end | Should Be '04:00'
    }
    It 'renova somente apos sete dias e dentro da janela local' {
        $policy=Get-HubBackupPolicy $TestDrive
        $inside=[DateTimeOffset](Get-Date -Year 2026 -Month 10 -Day 15 -Hour 3 -Minute 15 -Second 0)
        Test-HubFullRenewalWindow $policy ($inside.AddDays(-8)) $inside | Should Be $true
        Test-HubFullRenewalWindow $policy ($inside.AddDays(-6)) $inside | Should Be $false
        Test-HubFullRenewalWindow $policy ($inside.AddDays(-8)) ($inside.AddHours(2)) | Should Be $false
    }
    It 'aceita janela que cruza meia-noite e limite final exclusivo' {
        $policy=Get-HubBackupPolicy $TestDrive; $policy.window_start='23:00'; $policy.window_end='01:00'
        $now=[DateTimeOffset](Get-Date -Year 2026 -Month 10 -Day 15 -Hour 23 -Minute 30 -Second 0)
        Test-HubFullRenewalWindow $policy ($now.AddDays(-8)) $now | Should Be $true
        Test-HubFullRenewalWindow $policy ($now.AddDays(-8)) ($now.AddHours(1)) | Should Be $true
        $outside=[DateTimeOffset](Get-Date -Year 2026 -Month 10 -Day 16 -Hour 1 -Minute 0 -Second 0)
        Test-HubFullRenewalWindow $policy ($now.AddDays(-8)) $outside | Should Be $false
    }
    It 'rejeita configuracao invalida sem executar backup' {
        $root=Join-Path $TestDrive 'invalid-policy'; $null=New-Item -ItemType Directory -Path (Join-Path $root 'config')
        Write-HubBackupJson (Join-Path $root 'config\backup-policy.json') @{version=1;renewal_days=0}
        { Get-HubBackupPolicy $root } | Should Throw 'invalida'
    }
}

Describe 'Controle automatico sem backup real' {
    BeforeEach {
        $script:caseRoot=Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $script:installRoot=Join-Path $caseRoot 'app'; $script:dataRoot=Join-Path $caseRoot 'state'; $script:backupRoot=Join-Path $dataRoot 'backup'
        $null=New-Item -ItemType Directory -Path $backupRoot
        $script:identity=[pscustomobject]@{hub_uuid='22222222222242228222222222222222';empresa_id=1;loja_id=10;retaguarda_hub_id=20}
        $script:server=[pscustomobject]@{version='8.0.46';server_uuid='11111111-1111-4111-8111-111111111111';identity=$identity;datadir=(Join-Path $dataRoot 'mysql\data')}
        $script:fullName='full-20261001T120000000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        $script:fullDir=New-ControlTestFull $fullName
        $script:changed=$false; $script:renew=$false; $script:failMysql=$false; $script:failSource=$false; $script:failWindow=$false; $script:failWindowConnection=$false; $script:failCapture=$false; $script:failRenewal=$false; $script:metadataOnly=$false; $script:freeBytes=10GB; $script:storageBytes=1GB
        Mock Assert-HubBackupOperator { }
        Mock Assert-HubBackupService { }
        Mock Assert-HubMySqlToolchain { }
        Mock Assert-HubBackupCredential { }
        Mock Protect-SecretFile { }
        Mock Get-HubBackupServerState { if ($script:failMysql) { throw 'password=ficticio NAO PERSISTIR' }; return $script:server }
        Mock Get-HubBackupFreeBytes { $script:freeBytes }
        Mock Get-HubBackupStorageBytes { $script:storageBytes }
        Mock Assert-HubBinlogSource { if ($script:failSource) { throw 'Binlog ausente ou corrompido' } }
        Mock Get-HubBinlogWindow { if ($script:failWindowConnection) { $errorObject=New-Object IO.IOException('Conexao ficticia interrompida'); $errorObject.Data['HubBackupFailure']='MYSQL'; throw $errorObject }; if ($script:failWindow) { throw 'Lacuna de binlogs' }; return [pscustomobject]@{changed=$script:changed} }
        Mock Test-HubFullRenewalWindow { $script:renew }
        Mock Invoke-HubMySqlIncrementalBackup {
            $null=($BackupLock.CanWrite | Should Be $true)
            $null=($VerifiedChain.full.backup_id | Should Be (Split-Path $FullBackupDirectory -Leaf).Substring(25))
            if ($script:failCapture) { throw 'Falha de captura ficticia' }
            if ($script:metadataOnly) { return }
            $VerifiedChain.sequence++; $VerifiedChain.last_id='cccccccccccccccccccccccccccccccc'
            $name='incremental-' + $VerifiedChain.full.backup_id + '-' + ([int]$VerifiedChain.sequence).ToString('D8') + '-' + $VerifiedChain.last_id
            $dir=Join-Path $script:backupRoot $name; $null=New-Item -ItemType Directory -Path $dir
            Write-HubBackupJson (Join-Path $dir 'manifest.json') @{completed_at_utc=[DateTime]::UtcNow.ToString('o');archive=@{size_bytes=123}}
            return $dir
        }
        Mock Invoke-HubMySqlFullBackup {
            $null=($BackupLock.CanWrite | Should Be $true)
            if ($script:failRenewal) { throw 'Falha FULL ficticia' }
            return New-ControlTestFull 'full-20261015T120000000Z-dddddddddddddddddddddddddddddddd'
        }
    }
    It 'seleciona FULL automaticamente e nao captura sem mudanca' {
        $result=Invoke-HubBackupControl $installRoot $dataRoot
        $result.status | Should Be 'SEM_ALTERACOES'
        $result.active_full | Should Be $fullName
        $result.last_full.id | Should Be ('a'*32)
        Assert-MockCalled Invoke-HubMySqlIncrementalBackup -Times 0 -Exactly -Scope It
        Assert-MockCalled Invoke-HubMySqlFullBackup -Times 0 -Exactly -Scope It
        $stored=Read-HubBackupJson (Join-Path $backupRoot 'backup-control.json')
        $stored.last_attempt_utc | Should Not BeNullOrEmpty
        $stored.last_success_utc | Should Not BeNullOrEmpty
        $stored.elapsed_ms | Should BeGreaterThan 0
        $stored.storage_bytes | Should Be 1GB
    }
    It 'nao inicializa FULL nem incremental quando nao existe FULL' {
        $script:backupRoot=Join-Path $dataRoot 'empty'; $null=New-Item -ItemType Directory -Path $backupRoot
        # Outro ProgramData isolado, sem retirar qualquer backup anterior.
        $emptyRoot=Join-Path $caseRoot 'empty-state'; $null=New-Item -ItemType Directory -Path (Join-Path $emptyRoot 'backup')
        (Invoke-HubBackupControl $installRoot $emptyRoot).status | Should Be 'INICIALIZACAO_PENDENTE'
        Assert-MockCalled Invoke-HubMySqlFullBackup -Times 0 -Exactly -Scope It
        Assert-MockCalled Invoke-HubMySqlIncrementalBackup -Times 0 -Exactly -Scope It
    }
    It 'seleciona a cadeia FULL publicada mais recente sem exigir caminho' {
        $latest=New-ControlTestFull 'full-20261014T120000000Z-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
        (Invoke-HubBackupControl $installRoot $dataRoot).active_full | Should Be (Split-Path $latest -Leaf)
        Test-Path $fullDir | Should Be $true
    }
    It 'bloqueia FULL recente corrompido em vez de usar um antigo silenciosamente' {
        $latest=New-ControlTestFull 'full-20261014T120000000Z-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
        Add-Content (Join-Path $latest 'sysvarhub_db.sql.gz') 'corrupcao ficticia'
        (Invoke-HubBackupControl $installRoot $dataRoot).support_required | Should Be $true
        Assert-MockCalled Invoke-HubMySqlIncrementalBackup -Times 0 -Exactly -Scope It
    }
    It 'captura mudancas na cadeia selecionada e registra tamanho e ultimo incremental' {
        $script:changed=$true
        $result=Invoke-HubBackupControl $installRoot $dataRoot
        $result.status | Should Be 'INCREMENTAL_VALIDO'
        $result.published_bytes | Should Be 123
        $result.last_incremental.sequence | Should Be 1
        Assert-MockCalled Invoke-HubMySqlIncrementalBackup -Times 1 -Exactly -Scope It
    }
    It 'nao publica arquivo quando a mudanca contem somente metadados' {
        $script:changed=$true; $script:metadataOnly=$true
        (Invoke-HubBackupControl $installRoot $dataRoot).status | Should Be 'SEM_ALTERACOES'
        @(Get-ChildItem $backupRoot -Directory -Filter 'incremental-*').Count | Should Be 0
    }
    It 'renova FULL semanal validado sem substituir cadeia anterior' {
        $before=(Get-FileHash (Join-Path $fullDir 'sysvarhub_db.sql.gz')).Hash
        $script:renew=$true
        $result=Invoke-HubBackupControl $installRoot $dataRoot
        $result.status | Should Be 'FULL_RENOVADO'
        $result.active_full | Should Match 'dddddddd'
        (Get-FileHash (Join-Path $fullDir 'sysvarhub_db.sql.gz')).Hash | Should Be $before
        @(Get-ChildItem $backupRoot -Directory -Filter 'full-*').Count | Should Be 2
    }
    It 'falha na renovacao preserva selecao anterior e permite proxima tentativa' {
        $script:renew=$true; $script:failRenewal=$true
        (Invoke-HubBackupControl $installRoot $dataRoot).status | Should Be 'FALHA_REPETIR'
        $stored=Read-HubBackupJson (Join-Path $backupRoot 'backup-control.json')
        $stored.active_full | Should Be $fullName
        $script:failRenewal=$false
        (Invoke-HubBackupControl $installRoot $dataRoot).status | Should Be 'FULL_RENOVADO'
    }
    It 'MySQL indisponivel registra falha sanitizada e retoma na proxima execucao' {
        $script:failMysql=$true
        $first=Invoke-HubBackupControl $installRoot $dataRoot
        $first.status | Should Be 'FALHA_REPETIR'
        $first.support_required | Should Be $false
        [IO.File]::ReadAllText((Join-Path $backupRoot 'backup-control.json')) | Should Not Match 'password|NAO PERSISTIR'
        $script:failMysql=$false
        (Invoke-HubBackupControl $installRoot $dataRoot).status | Should Be 'SEM_ALTERACOES'
    }
    It 'falha transitoria ao consultar coordenadas nao vira bloqueio de cadeia' {
        $script:failWindowConnection=$true
        $first=Invoke-HubBackupControl $installRoot $dataRoot
        $first.status | Should Be 'FALHA_REPETIR'
        $first.support_required | Should Be $false
        $script:failWindowConnection=$false
        (Invoke-HubBackupControl $installRoot $dataRoot).status | Should Be 'SEM_ALTERACOES'
    }
    It 'falha de captura nao muda selecao nem sucesso anterior' {
        $first=Invoke-HubBackupControl $installRoot $dataRoot
        $script:changed=$true; $script:failCapture=$true
        $result=Invoke-HubBackupControl $installRoot $dataRoot
        $result.status | Should Be 'FALHA_REPETIR'
        $result.active_full | Should Be $fullName
        $result.last_success_utc | Should Be $first.last_success_utc
    }
    It 'falta de espaco bloqueia trabalho pesado e preserva backup' {
        $script:freeBytes=100MB; $script:renew=$true; $script:changed=$true
        (Invoke-HubBackupControl $installRoot $dataRoot).status | Should Be 'ESPACO_INSUFICIENTE'
        Test-Path $fullDir | Should Be $true
        Assert-MockCalled Get-HubBackupServerState -Times 0 -Exactly -Scope It
        Assert-MockCalled Invoke-HubMySqlFullBackup -Times 0 -Exactly -Scope It
    }
    It 'alerta crescimento excessivo sem apagar arquivos' {
        $script:storageBytes=70GB
        $result=Invoke-HubBackupControl $installRoot $dataRoot
        $result.warnings | Should Be 'ARMAZENAMENTO_ELEVADO_ACIONAR_SUPORTE'
        Test-Path $fullDir | Should Be $true
    }
    It 'capturas simultaneas nao alteram diagnostico nem executam backup' {
        $prior=Invoke-HubBackupControl $installRoot $dataRoot
        $hash=(Get-FileHash (Join-Path $backupRoot 'backup-control.json')).Hash
        $held=Enter-HubBackupLock $backupRoot
        try { (Invoke-HubBackupControl $installRoot $dataRoot).status | Should Be 'EM_EXECUCAO' }
        finally { $held.Dispose() }
        (Get-FileHash (Join-Path $backupRoot 'backup-control.json')).Hash | Should Be $hash
    }
    It 'origem corrompida bloqueia renovacao e exige suporte mesmo apos retry' {
        $script:failSource=$true; $script:renew=$true
        (Invoke-HubBackupControl $installRoot $dataRoot).support_required | Should Be $true
        $script:failSource=$false
        (Invoke-HubBackupControl $installRoot $dataRoot).status | Should Be 'INTERVENCAO_SUPORTE'
        Assert-MockCalled Invoke-HubMySqlFullBackup -Times 0 -Exactly -Scope It
        (Invoke-HubBackupControl $installRoot $dataRoot -ResumeAfterSupport).status | Should Be 'FULL_RENOVADO'
    }
    It 'lacuna de binlogs bloqueia avanço sem gerar FULL para oculta-la' {
        $script:failWindow=$true; $script:renew=$true
        (Invoke-HubBackupControl $installRoot $dataRoot).support_required | Should Be $true
        Assert-MockCalled Invoke-HubMySqlFullBackup -Times 0 -Exactly -Scope It
        Assert-MockCalled Invoke-HubMySqlIncrementalBackup -Times 0 -Exactly -Scope It
    }
    It 'nao adota FULL novo para mascarar corrupcao da cadeia ativa' {
        $null=Invoke-HubBackupControl $installRoot $dataRoot
        Add-Content (Join-Path $fullDir 'sysvarhub_db.sql.gz') 'corrupcao ficticia'
        $null=New-ControlTestFull 'full-20261015T120000000Z-dddddddddddddddddddddddddddddddd'
        $result=Invoke-HubBackupControl $installRoot $dataRoot
        $result.support_required | Should Be $true
        $result.active_full | Should Be $fullName
    }
    It 'preserva controle corrompido para intervencao em vez de reinicializar' {
        $path=Join-Path $backupRoot 'backup-control.json'; Set-Content $path 'JSON INVALIDO FICTICIO'
        (Invoke-HubBackupControl $installRoot $dataRoot).support_required | Should Be $true
        (Get-Content $path) | Should Be 'JSON INVALIDO FICTICIO'
    }
    It 'adota FULL manual mais recente somente depois de validar cadeia ativa' {
        $null=Invoke-HubBackupControl $installRoot $dataRoot
        $new=New-ControlTestFull 'full-20261015T120000000Z-dddddddddddddddddddddddddddddddd'
        (Invoke-HubBackupControl $installRoot $dataRoot).active_full | Should Be (Split-Path $new -Leaf)
        Assert-MockCalled Assert-HubBinlogSource -Times 3 -Exactly -Scope It
    }
    It 'mantem trava do controlador sob posse do chamador' {
        $held=Enter-HubBackupLock $backupRoot
        try {
            $returned=Get-HubBackupOperationLock $backupRoot $held
            [object]::ReferenceEquals($held,$returned) | Should Be $true
            { Enter-HubBackupLock $backupRoot } | Should Throw 'em andamento'
        } finally { $held.Dispose() }
    }
    It 'rejeita handle compartilhado como trava externa' {
        $shared=[IO.File]::Open((Join-Path $backupRoot '.full-backup.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::ReadWrite)
        try { { Get-HubBackupOperationLock $backupRoot $shared } | Should Throw 'nao e exclusiva' }
        finally { $shared.Dispose() }
    }
    It 'servico DEV ou ausente nunca chega a consultar ou gerar backup' {
        Mock Assert-HubBackupService { throw 'MySQL80/DEV nao e permitido' }
        (Invoke-HubBackupControl $installRoot $dataRoot).status | Should Be 'FALHA_REPETIR'
        Assert-MockCalled Get-HubBackupServerState -Times 0 -Exactly -Scope It
        Assert-MockCalled Invoke-HubMySqlFullBackup -Times 0 -Exactly -Scope It
    }
}

Describe 'Classificacao de consulta sem MySQL real' {
    It 'marca falha nativa como temporaria sem usar outra conta' {
        Mock Invoke-HubMySqlProcess { throw 'Falha de conexao inteiramente ficticia' }
        $caught=$null
        try { $null=Get-HubBackupQuery 'C:\mysql-ficticio' 'C:\credencial-ficticia.cnf' 'SHOW MASTER STATUS;' }
        catch { $caught=$_.Exception }
        $caught.Data['HubBackupFailure'] | Should Be 'MYSQL'
    }
}
