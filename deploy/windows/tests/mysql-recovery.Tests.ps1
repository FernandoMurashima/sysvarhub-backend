$ErrorActionPreference='Stop'
$WindowsRoot=Split-Path $PSScriptRoot -Parent
. (Join-Path $WindowsRoot 'mysql-recovery.ps1')

Describe 'Preparacao explicita e bloqueio do Hub' {
    BeforeEach {
        $script:dataRoot=Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        Mock Assert-HubBackupOperator {}
        Mock Protect-SecretFile {}
        Mock Get-Service { [pscustomobject]@{Status='Stopped';StartType='Disabled'} }
        Mock Set-Service {}
        Mock Start-Service { throw 'Nao pode iniciar servico no teste.' }
        Mock Stop-Service { throw 'Nao pode parar servico no teste.' }
    }
    It 'nao prepara sem autorizacao explicita' {
        { Initialize-HubRecoveryMode $dataRoot } | Should Throw 'confirmacao explicita'
        (Test-Path $dataRoot) | Should Be $false
        Assert-MockCalled Set-Service -Scope It -Times 0 -Exactly
    }
    It 'mantem marcador persistente e servico desativado sem iniciar ou reiniciar' {
        $marker=Initialize-HubRecoveryMode $dataRoot -SupportConfirmed
        (Test-Path -LiteralPath $marker) | Should Be $true
        Assert-MockCalled Set-Service -Scope It -Times 1 -Exactly -ParameterFilter { $Name -eq 'SysvarHub' -and $StartupType -eq 'Disabled' }
        Assert-MockCalled Start-Service -Scope It -Times 0 -Exactly
        Assert-MockCalled Stop-Service -Scope It -Times 0 -Exactly
    }
    It 'recusa Hub em execucao sem alterar o servico' {
        Mock Get-Service { [pscustomobject]@{Status='Running';StartType='Automatic'} }
        { Initialize-HubRecoveryMode $dataRoot -SupportConfirmed } | Should Throw 'parado'
        Assert-MockCalled Set-Service -Scope It -Times 0 -Exactly
    }
    It 'preserva bloqueio e diagnostico preexistentes' {
        $marker=Initialize-HubRecoveryMode $dataRoot -SupportConfirmed
        [IO.File]::WriteAllText($marker,'{"status":"FALHA_SUPORTE"}')
        $null=Initialize-HubRecoveryMode $dataRoot -SupportConfirmed
        [IO.File]::ReadAllText($marker) | Should Be '{"status":"FALHA_SUPORTE"}'
    }
}

Describe 'Conta assistida distinta da conta de backup' {
    BeforeEach {
        $script:credential=Join-Path $TestDrive 'restore.cnf'
        @('[client]','user=sysvarhub_restore','password=fictitious-test-only','host=127.0.0.1','port=3307','protocol=TCP') | Set-Content $credential
        $script:acl=[pscustomobject]@{AreAccessRulesProtected=$true}
        $acl | Add-Member ScriptMethod GetAccessRules { param($a,$b,$c) @([pscustomobject]@{AccessControlType='Allow';IdentityReference=[pscustomobject]@{Value='S-1-5-18'}}) }
        Mock Get-Acl { $script:acl }
    }
    It 'aceita credencial exclusiva de restauracao e preserva validacao de backup' {
        { Assert-HubBackupCredential $credential -ExpectedUser sysvarhub_restore } | Should Not Throw
        { Assert-HubBackupCredential $credential } | Should Throw 'sysvarhub_backup'
    }
    It 'rejeita root, conta de backup e porta DEV para restauracao' {
        foreach ($user in @('root','sysvarhub_backup')) {
            @('[client]',"user=$user",'password=fictitious-only','host=127.0.0.1','port=3307','protocol=TCP') | Set-Content $credential
            { Assert-HubBackupCredential $credential -ExpectedUser sysvarhub_restore } | Should Throw 'exclusivamente'
        }
        @('[client]','user=sysvarhub_restore','password=fictitious-only','host=127.0.0.1','port=3306','protocol=TCP') | Set-Content $credential
        { Assert-HubBackupCredential $credential -ExpectedUser sysvarhub_restore } | Should Throw '3307'
    }
    It 'rejeita credencial sem ACL e opcoes livres' {
        $acl.AreAccessRulesProtected=$false
        { Assert-HubBackupCredential $credential -ExpectedUser sysvarhub_restore } | Should Throw 'ACL'
        $acl.AreAccessRulesProtected=$true
        Add-Content $credential '!include arbitrary.cnf'
        { Assert-HubBackupCredential $credential -ExpectedUser sysvarhub_restore } | Should Throw 'Formato'
    }
    It 'rejeita pacote exposto em diretorio publico' {
        $directory=Join-Path $TestDrive 'fictitious-package'
        $null=New-Item -ItemType Directory -Path $directory -Force
        Set-Content -LiteralPath (Join-Path $directory 'pacote.json') '{}'
        { Assert-HubRecoveryPrivatePackage $directory } | Should Not Throw
        $acl.AreAccessRulesProtected=$false
        { Assert-HubRecoveryPrivatePackage $directory } | Should Throw 'privado'
    }
    It 'exige privilegios de aplicacao, sessao e visibilidade completa sem acesso a outros bancos' {
        $global='GRANT PROCESS, SHOW DATABASES, REPLICATION_APPLIER, SESSION_VARIABLES_ADMIN ON *.* TO `sysvarhub_restore`@`127.0.0.1`'
        $schema='GRANT ALL PRIVILEGES ON `sysvarhub\_db`.* TO `sysvarhub_restore`@`127.0.0.1`'
        { Assert-HubRecoveryGrants ($global+"`n"+$schema) $false } | Should Not Throw
        { Assert-HubRecoveryGrants ($global+"`n"+$schema.Replace('sysvarhub\_db','other')) $false } | Should Throw 'outros bancos'
        { Assert-HubRecoveryGrants ($global.Replace('SESSION_VARIABLES_ADMIN','SUPER')+"`n"+$schema) $false } | Should Throw 'excessivos'
        { Assert-HubRecoveryGrants ($global.Replace('PROCESS, ','')+"`n"+$schema) $false } | Should Throw 'minimos'
    }
}

Describe 'Orquestracao somente com simulacao de MySQL e runtime' {
    BeforeEach {
        $script:root=Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $null=New-Item -ItemType Directory -Path (Join-Path $root 'config') -Force
        [IO.File]::WriteAllText((Join-Path $root 'config\recovery-block.json'),'{}')
        $script:params=@{PackageDirectory=$root;ManifestSha256=('a'*64);PointUuid=[guid]'11111111-1111-4111-8111-111111111111';EmpresaId=1;LojaId=20;InstallationUuid=[guid]'22222222-2222-4222-8222-222222222222';SourceServerUuid=[guid]'33333333-3333-4333-8333-333333333333';InstallRoot=$root;ProgramDataRoot=$root}
        Mock Assert-HubBackupOperator {}
        Mock Assert-HubRecoveryPrivatePackage {}
        Mock Get-Service { [pscustomobject]@{Status='Stopped';StartType='Disabled'} }
        Mock Assert-HubBackupService {}
        Mock Assert-HubMySqlToolchain { $root }
        Mock Assert-HubBackupCredential {}
        Mock Assert-HubRecoveryGrants {}
        Mock Get-HubMySqlBinaryVersion { [version]'8.0.46' }
        Mock Protect-SecretFile {}
        Mock Invoke-HubMySqlProcess { if ($InputSql -match 'partial_revokes') { '0' } else { 'GRANT FICTITIOUS' } }
        Mock Invoke-HubRecoveryRuntime {
            $script:plan=Read-HubBackupJson $PlanPath
            [pscustomobject]@{status=$(if ($Execute) { 'RESTAURADO_BLOQUEADO' } else { 'DRY_RUN_APROVADO' });pdv_liberado=$false}
        }
        Mock Start-Service { throw 'Nao iniciar servicos.' }
    }
    It 'usa DRY-RUN por padrao e nao executa importacao' {
        (Invoke-HubMySqlRecovery @params).status | Should Be 'DRY_RUN_APROVADO'
        Assert-MockCalled Invoke-HubRecoveryRuntime -Scope It -Times 1 -Exactly -ParameterFilter { -not $Execute }
        Assert-MockCalled Start-Service -Scope It -Times 0 -Exactly
        $plan.point_uuid | Should Be $params.PointUuid.ToString()
        ($plan | ConvertTo-Json -Compress) | Should Not Match 'password|token'
    }
    It 'nao executa sem confirmacao exata' {
        { Invoke-HubMySqlRecovery @params -Execute -Confirmation 'SIM' } | Should Throw 'confirmacao explicita'
        Assert-MockCalled Invoke-HubRecoveryRuntime -Scope It -Times 0 -Exactly
    }
    It 'envia autorizacao vinculada e retomada somente quando explicita' {
        $confirm='RESTAURAR:'+$params.PointUuid.ToString()+':'+$params.ManifestSha256
        (Invoke-HubMySqlRecovery @params -Execute -Confirmation $confirm -ResumeAfterSupport).status | Should Be 'RESTAURADO_BLOQUEADO'
        $plan.resume_after_support | Should Be $true
        Assert-MockCalled Start-Service -Scope It -Times 0 -Exactly
    }
    It 'bloqueia instancia incorreta antes de importar' {
        Mock Assert-HubBackupService { throw 'MySQL80 DEV bloqueado' }
        { Invoke-HubMySqlRecovery @params } | Should Throw 'DEV bloqueado'
        Assert-MockCalled Invoke-HubRecoveryRuntime -Scope It -Times 0 -Exactly
    }
    It 'falha preserva bloqueio e servicos parados' {
        Mock Invoke-HubRecoveryRuntime { throw 'Falha ficticia durante importacao' }
        { Invoke-HubMySqlRecovery @params -Execute -Confirmation ('RESTAURAR:'+$params.PointUuid.ToString()+':'+$params.ManifestSha256) } | Should Throw 'Falha ficticia'
        (Test-Path -LiteralPath (Join-Path $root 'config\recovery-block.json')) | Should Be $true
        Assert-MockCalled Start-Service -Scope It -Times 0 -Exactly
    }
    It 'exige servico parado e desativado' {
        Mock Get-Service { [pscustomobject]@{Status='Stopped';StartType='Automatic'} }
        { Invoke-HubMySqlRecovery @params } | Should Throw 'desativado'
        Assert-MockCalled Invoke-HubRecoveryRuntime -Scope It -Times 0 -Exactly
    }
    It 'exige preparacao persistente' {
        Remove-Item -LiteralPath (Join-Path $root 'config\recovery-block.json')
        { Invoke-HubMySqlRecovery @params } | Should Throw 'Prepare primeiro'
        Assert-MockCalled Invoke-HubRecoveryRuntime -Scope It -Times 0 -Exactly
    }
}

Describe 'Integracao do modo explicito ao instalador' {
    It 'mantem modo normal e retorna antes de migrations e inicio de Hub na recuperacao' {
        $text=[IO.File]::ReadAllText((Join-Path $WindowsRoot 'install-hub.ps1'))
        $text | Should Match '\[switch\]\$RecoveryMode'
        $text | Should Match 'elseif \(Test-Path -LiteralPath \$RecoveryMarker\)'
        $exit=$text.IndexOf("Write-Host 'MYSQL_PREPARADO_HUB_BLOQUEADO")
        ($exit -gt 0 -and $exit -lt $text.IndexOf('& $ServiceExe manage migrate')) | Should Be $true
        $text.Substring($exit,220) | Should Match 'return'
        $text | Should Match '\$probe.objects -ne 0'
        $text | Should Not Match '(?i)\b(?:DROP DATABASE|RESET MASTER|PURGE BINARY LOGS)\b'
    }
    It 'instalador distribuido exige dois parametros explicitos para o novo modo' {
        $text=[IO.File]::ReadAllText((Join-Path $WindowsRoot 'installer\SysvarHubSetup.iss'))
        $text | Should Match 'param:RECOVERY\|0'
        $text | Should Match 'param:CONFIRMRECOVERY\|0'
        $text | Should Match ' -RecoveryMode -ConfirmRecoveryPreparation'
    }
}
