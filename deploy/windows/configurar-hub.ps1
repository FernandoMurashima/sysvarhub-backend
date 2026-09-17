param(
    [string]$ServiceExe = "C:\Program Files\Sysvar Hub\runtime\SysvarHubService.exe"
)

$ErrorActionPreference = "Stop"

$CentralUrl = Read-Host "URL da Central"
$Codigo = Read-Host "Codigo de ativacao"

& $ServiceExe manage ativar_hub --codigo $Codigo --url $CentralUrl
if ($LASTEXITCODE -ne 0) { throw "Ativacao falhou." }

$commands = @(
    "sincronizar_bootstrap_hub",
    "sincronizar_catalogo_hub",
    "sincronizar_operadores_hub",
    "sincronizar_vendedores_hub",
    "sincronizar_formas_pagamento_hub",
    "sincronizar_tipos_despesa_pdv_hub",
    "sincronizar_clientes_hub"
)

foreach ($command in $commands) {
    & $ServiceExe manage $command
    if ($LASTEXITCODE -ne 0) {
        throw "Carga inicial falhou no comando $command. Execute este configurador novamente depois."
    }
}

Write-Host "Sysvar Hub ativado e carga inicial concluida."
