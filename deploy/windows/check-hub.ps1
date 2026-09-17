param(
    [string]$Url = "http://127.0.0.1:8000/api/health/",
    [int]$WaitSeconds = 0,
    [int]$IntervalSeconds = 2
)

$ErrorActionPreference = "Stop"

$deadline = (Get-Date).AddSeconds([Math]::Max(0, $WaitSeconds))
$lastError = $null

do {
    try {
        $response = Invoke-RestMethod -Method Get -Uri $Url -TimeoutSec 10
        if ($response.status -eq "ok" -and $response.service -eq "sysvar-hub") {
            Write-Host "Hub saudavel."
            exit 0
        }

        $lastError = "Resposta inesperada do health check."
    } catch {
        $lastError = "Health check falhou: $($_.Exception.Message)"
    }

    if ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds ([Math]::Max(1, $IntervalSeconds))
    }
} while ((Get-Date) -lt $deadline)

Write-Error $lastError
exit 1
