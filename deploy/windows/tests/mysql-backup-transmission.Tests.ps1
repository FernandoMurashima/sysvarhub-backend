$ErrorActionPreference='Stop'
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'mysql-backup-transmission.ps1')

Describe 'Transmissao opt-in e isolada da geracao' {
    BeforeEach {
        $script:dataRoot=Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $script:installRoot=Join-Path $dataRoot 'app'
        $null=New-Item -ItemType Directory -Path (Join-Path $dataRoot 'backup')
        $null=New-Item -ItemType Directory -Path (Join-Path $dataRoot 'config')
        $script:failService=$false; $script:failPlan=$false; $script:free=1GB
        $script:policy=[pscustomobject]@{version=1;enabled=$true;bandwidth_bytes_per_second=262144;cycle_seconds=120;request_timeout_seconds=30;finalize_timeout_seconds=300}
        Mock Assert-HubBackupOperator { }
        Mock Protect-SecretFile { }
        Mock Get-HubBackupFreeBytes { $script:free }
        Mock Get-HubBackupTransmissionPlan {
            if ($script:failPlan) { throw 'Nao persistir password=FAKE' }
            return @{version=1;directories=@('full-ficticio');backup_root=(Join-Path $ProgramDataRoot 'backup');policy=$Policy}
        }
        Mock Invoke-HubBackupTransmissionRuntime {
            $plan=Read-HubBackupJson $PlanPath
            $null=($plan.policy.enabled | Should Be $true)
            $null=($env:SYSVARHUB_PROGRAMDATA | Should Be $script:dataRoot)
            return 'RECEBIDO_CENTRAL'
        }
    }
    It 'permanece desativada por padrao e nao consulta MySQL nem runtime' {
        Invoke-HubBackupTransmission $installRoot $dataRoot | Should Be 'DESATIVADO'
        Assert-MockCalled Get-HubBackupTransmissionPlan -Times 0 -Exactly -Scope It
        Assert-MockCalled Invoke-HubBackupTransmissionRuntime -Times 0 -Exactly -Scope It
        @(Get-ChildItem (Join-Path $dataRoot 'backup') -Force).Count | Should Be 0
    }
    It 'transmite somente quando explicitamente habilitada sem mudar ambiente permanente' {
        Write-HubBackupJson (Join-Path $dataRoot 'config\backup-transmission.json') $policy
        $before=$env:SYSVARHUB_PROGRAMDATA
        Invoke-HubBackupTransmission $installRoot $dataRoot | Should Be 'RECEBIDO_CENTRAL'
        $env:SYSVARHUB_PROGRAMDATA | Should Be $before
        @(Get-ChildItem (Join-Path $dataRoot 'backup') -Filter '.send-plan-*' -Force).Count | Should Be 0
        Assert-MockCalled Invoke-HubBackupTransmissionRuntime -Times 1 -Exactly -Scope It
    }
    It 'falta de espaco impede envio sem remover backups' {
        Write-HubBackupJson (Join-Path $dataRoot 'config\backup-transmission.json') $policy
        Set-Content (Join-Path $dataRoot 'backup\valid-backup.fixture') 'ficticio'
        $script:free=0
        Invoke-HubBackupTransmission $installRoot $dataRoot | Should Be 'INTERVENCAO_SUPORTE'
        Test-Path (Join-Path $dataRoot 'backup\valid-backup.fixture') | Should Be $true
        Assert-MockCalled Invoke-HubBackupTransmissionRuntime -Times 0 -Exactly -Scope It
    }
    It 'cadeia invalida nao chama transmissor nem vaza mensagem nativa' {
        Write-HubBackupJson (Join-Path $dataRoot 'config\backup-transmission.json') $policy
        $script:failPlan=$true
        Invoke-HubBackupTransmission $installRoot $dataRoot | Should Be 'INTERVENCAO_SUPORTE'
        $text=Get-Content (Join-Path $dataRoot 'backup\backup-transmission-preflight.json') -Raw
        $text | Should Not Match 'password|FAKE'
        Assert-MockCalled Invoke-HubBackupTransmissionRuntime -Times 0 -Exactly -Scope It
    }
    It 'rejeita limites invalidos e falso textual em vez de habilitar implicitamente' {
        $policy.enabled='false'
        Write-HubBackupJson (Join-Path $dataRoot 'config\backup-transmission.json') $policy
        {Get-HubBackupTransmissionPolicy $dataRoot} | Should Throw 'invalida'
    }
    It 'mantem geracao e transmissao separadas sem alterar agendamento' {
        $scriptText=Get-Content (Join-Path (Split-Path $PSScriptRoot -Parent) 'backup-run.ps1') -Raw
        $scriptText.IndexOf('Invoke-HubBackupControl') | Should BeLessThan $scriptText.IndexOf('Invoke-HubBackupTransmission -')
        $scriptText | Should Match "SEM_ALTERACOES.*INCREMENTAL_VALIDO.*FULL_RENOVADO"
        $scriptText | Should Not Match 'Register-ScheduledTask|Enable-ScheduledTask|Start-ScheduledTask'
    }
}

Describe 'Selecao reutiliza a cadeia validada e bloqueia instancia DEV' {
    BeforeEach {
        $script:dataRoot=Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $script:backupRoot=Join-Path $dataRoot 'backup'; $null=New-Item -ItemType Directory -Path $backupRoot
        $script:fullId='a'*32
        $script:fullName='full-20261001T120000000Z-' + $fullId
        $null=New-Item -ItemType Directory -Path (Join-Path $backupRoot $fullName)
        $script:dev=$false; $script:badChain=$false
        Mock Assert-HubBackupService { if ($script:dev) { throw 'MySQL80/3306 bloqueado' } }
        Mock Assert-HubMySqlToolchain { }
        Mock Assert-HubBackupCredential { }
        Mock Get-HubBackupServerState { [pscustomobject]@{server_uuid='11111111-1111-4111-8111-111111111111'} }
        Mock Get-HubIncrementalChain {
            if ($script:badChain) { throw 'Cadeia interrompida' }
            [pscustomobject]@{full=[pscustomobject]@{backup_id=$script:fullId}}
        }
    }
    It 'ordena FULL antes dos incrementais e libera trava de geracao' {
        $inc='incremental-' + $fullId + '-00000001-' + ('b'*32)
        $null=New-Item -ItemType Directory -Path (Join-Path $backupRoot $inc)
        $plan=Get-HubBackupTransmissionPlan 'C:\fake-app' $dataRoot @{}
        $plan.directories[0] | Should Be $fullName
        $plan.directories[1] | Should Be $inc
        $plan.service | Should Be 'SysvarHubMySQL'
        $plan.port | Should Be 3307
        Assert-MockCalled Get-HubIncrementalChain -Times 1 -Exactly -Scope It
        $lock=Enter-HubBackupLock $backupRoot; $lock.Dispose()
    }
    It 'bloqueia DEV antes de ler metadados do MySQL' {
        $script:dev=$true
        {Get-HubBackupTransmissionPlan 'C:\fake-app' $dataRoot @{}} | Should Throw '3306'
        Assert-MockCalled Get-HubBackupServerState -Times 0 -Exactly -Scope It
    }
    It 'bloqueia cadeia com lacuna antes de montar plano' {
        $script:badChain=$true
        {Get-HubBackupTransmissionPlan 'C:\fake-app' $dataRoot @{}} | Should Throw 'Cadeia'
    }
    It 'rejeita incremental sem FULL correspondente' {
        $null=New-Item -ItemType Directory -Path (Join-Path $backupRoot ('incremental-' + ('c'*32) + '-00000001-' + ('d'*32)))
        {Get-HubBackupTransmissionPlan 'C:\fake-app' $dataRoot @{}} | Should Throw 'sem FULL'
    }
}


Describe 'Sintaxe e protecao do novo transmissor' {
    It 'analisa em Windows PowerShell 5.1 sem executar runtime ou MySQL' {
        foreach ($name in @('mysql-backup-transmission.ps1','backup-run.ps1','mysql-backup-task.ps1')) {
            $tokens=$null; $errors=$null
            $null=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Split-Path $PSScriptRoot -Parent) $name),[ref]$tokens,[ref]$errors)
            @($errors).Count | Should Be 0
        }
        $PSVersionTable.PSVersion.Major | Should Be 5
    }
}
