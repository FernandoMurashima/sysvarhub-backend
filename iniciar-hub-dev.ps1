$ErrorActionPreference = "Stop"

$backend = $PSScriptRoot
$frontend = [System.IO.Path]::GetFullPath(
    (Join-Path $backend "..\Frontend")
)
$runtimeDev = Join-Path $backend ".runtime-dev"
$python = Join-Path $backend ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Venv do Hub não encontrado."
}

if (-not (Test-Path (Join-Path $frontend "package.json"))) {
    throw "Frontend Hub não encontrado."
}

# Ambiente de desenvolvimento do Hub
$env:SYSVARHUB_PROGRAMDATA = $runtimeDev

$service = Get-Service -Name "SysvarHub" -ErrorAction SilentlyContinue
if ($service -and $service.Status -eq "Running") {
    throw "Serviço Windows SysvarHub está em execução. Pare o serviço instalado antes de iniciar o ambiente DEV para evitar worker duplicado."
}

Write-Host "Aplicando migrations do Hub DEV..."
& $python manage.py migrate

if ($LASTEXITCODE -ne 0) {
    throw "Falha ao aplicar migrations do Hub DEV."
}

$backendCommand = "Set-Location -LiteralPath '$backend'; `$env:SYSVARHUB_PROGRAMDATA = '$runtimeDev'; & '$python' manage.py runserver 127.0.0.1:8100"
$frontendCommand = "Set-Location -LiteralPath '$frontend'; npm start"
$workerCommand = "Set-Location -LiteralPath '$backend'; `$env:SYSVARHUB_PROGRAMDATA = '$runtimeDev'; & '$python' manage.py run_sync_worker"

Start-Process powershell.exe -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $backendCommand
Start-Process powershell.exe -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $frontendCommand
Start-Process powershell.exe -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $workerCommand
