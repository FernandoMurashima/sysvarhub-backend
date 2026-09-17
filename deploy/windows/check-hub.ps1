param(
    [string]$Url = "http://127.0.0.1:8000/api/health/"
)

$ErrorActionPreference = "Stop"

try {
    $response = Invoke-RestMethod -Method Get -Uri $Url -TimeoutSec 10
    if ($response.status -eq "ok" -and $response.service -eq "sysvar-hub") {
        Write-Host "Hub saudavel."
        exit 0
    }

    Write-Error "Resposta inesperada do health check."
    exit 2
} catch {
    Write-Error "Health check falhou: $($_.Exception.Message)"
    exit 1
}
