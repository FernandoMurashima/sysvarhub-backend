param(
    [string]$ServiceExe = "C:\Program Files\Sysvar Hub\runtime\SysvarHubService.exe"
)

$ErrorActionPreference = "Stop"

$CentralUrl = Read-Host "URL da Central"
$Codigo = Read-Host "Codigo de ativacao"

& $ServiceExe manage ativar_hub --codigo $Codigo --url $CentralUrl
if ($LASTEXITCODE -ne 0) { throw "Ativacao falhou." }

Write-Host "Sysvar Hub ativado com sucesso."
Write-Host "Aguardando primeira sincronizacao comandada pela Central."
