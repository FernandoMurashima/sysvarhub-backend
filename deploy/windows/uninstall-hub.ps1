param(
    [string]$InstallRoot = "C:\Program Files\Sysvar Hub"
)

$ErrorActionPreference = "Stop"

$ServiceStopTimeoutSeconds = 45
$ForcedHubStopTimeoutSeconds = 20

function Get-ServiceStatus([string]$Name) {
    $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $service) { return $null }
    return $service.Status
}

function Wait-ServiceStopped {
    param(
        [string]$Name,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $status = Get-ServiceStatus $Name
        if ($null -eq $status -or $status -eq "Stopped") { return $true }
        Start-Sleep -Seconds 1
    }
    return ((Get-ServiceStatus $Name) -eq "Stopped")
}

function Stop-ServiceWithTimeout {
    param(
        [string]$Name,
        [int]$TimeoutSeconds = $ServiceStopTimeoutSeconds,
        [switch]$AllowServicePidKill
    )

    $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $service) { return }
    if ($service.Status -eq "Stopped") { return }

    Write-Host "Parando servico $Name..."
    Stop-Service -Name $Name -ErrorAction SilentlyContinue
    if (Wait-ServiceStopped -Name $Name -TimeoutSeconds $TimeoutSeconds) { return }

    $status = Get-ServiceStatus $Name
    if ($AllowServicePidKill -and $Name -eq "SysvarHub" -and $status -eq "StopPending") {
        $wmiService = Get-CimInstance Win32_Service -Filter "Name='$Name'" -ErrorAction SilentlyContinue
        $serviceProcessId = 0
        if ($wmiService) { $serviceProcessId = [int]$wmiService.ProcessId }
        if ($serviceProcessId -gt 0) {
            Write-Host "Servico $Name permaneceu em Stop Pending. Encerrando somente o PID associado ao servico: $serviceProcessId."
            Stop-Process -Id $serviceProcessId -Force -ErrorAction Stop
            if (Wait-ServiceStopped -Name $Name -TimeoutSeconds $ForcedHubStopTimeoutSeconds) { return }
        }
    }

    throw "Nao foi possivel parar o servico $Name dentro de $TimeoutSeconds segundos. Estado atual: $(Get-ServiceStatus $Name)."
}

function Remove-ServiceAfterStop {
    param(
        [string]$Name,
        [switch]$AllowServicePidKill
    )

    $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $service) { return }

    Stop-ServiceWithTimeout -Name $Name -AllowServicePidKill:$AllowServicePidKill
    if ((Get-ServiceStatus $Name) -ne "Stopped") {
        throw "Servico $Name nao confirmou estado Stopped; exclusao abortada."
    }

    $output = & sc.exe delete $Name 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0 -and ($output -notmatch "1060")) {
        throw "Falha ao excluir servico $Name via sc.exe delete. ExitCode=$exitCode"
    }
}

Remove-ServiceAfterStop -Name "SysvarHub" -AllowServicePidKill
Remove-ServiceAfterStop -Name "SysvarHubMySQL"

Get-NetFirewallRule -DisplayName "Sysvar Hub" -ErrorAction SilentlyContinue | Remove-NetFirewallRule

Write-Host "Limpeza operacional concluida. O Inno Setup removera os arquivos em Program Files. C:\ProgramData\SysvarHub foi preservado."
