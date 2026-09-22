$ErrorActionPreference = "Stop"

$backend = $PSScriptRoot
$frontend = [System.IO.Path]::GetFullPath(
    (Join-Path $backend "..\Frontend")
)
$runtimeDev = Join-Path $backend ".runtime-dev"

if (-not (Test-Path (Join-Path $backend ".venv\Scripts\python.exe"))) {
    throw "Venv do Hub não encontrado."
}

if (-not (Test-Path (Join-Path $frontend "package.json"))) {
    throw "Frontend Hub não encontrado."
}

$backendCommand = "Set-Location -LiteralPath '$backend'; `$env:SYSVARHUB_PROGRAMDATA = '$runtimeDev'; & '.\.venv\Scripts\python.exe' manage.py runserver 127.0.0.1:8100"
$frontendCommand = "Set-Location -LiteralPath '$frontend'; npm start"

Start-Process powershell.exe -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $backendCommand
Start-Process powershell.exe -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $frontendCommand