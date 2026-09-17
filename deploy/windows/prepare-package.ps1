param(
    [string]$BackendRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$StagingRoot = (Join-Path $PSScriptRoot "staging")
)

$ErrorActionPreference = "Stop"

$resolvedBackend = (Resolve-Path $BackendRoot).Path
$resolvedStagingParent = (Resolve-Path (Split-Path $StagingRoot -Parent)).Path

if (Test-Path $StagingRoot) {
    $resolvedStaging = (Resolve-Path $StagingRoot).Path
    if (-not $resolvedStaging.StartsWith($resolvedStagingParent)) {
        throw "StagingRoot fora do diretorio esperado: $resolvedStaging"
    }
    Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
}

New-Item -ItemType Directory -Force -Path `
    (Join-Path $StagingRoot "app"), `
    (Join-Path $StagingRoot "frontend"), `
    (Join-Path $StagingRoot "config-template"), `
    (Join-Path $StagingRoot "scripts") | Out-Null

$appTarget = Join-Path $StagingRoot "app"
$robocopyArgs = @(
    $resolvedBackend,
    $appTarget,
    "/E",
    "/XD",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "staging",
    "node_modules",
    "/XF",
    "*.pyc",
    "*.pyo",
    "*.sqlite3",
    ".env",
    "tests.py",
    "tests_*.py",
    "*_tests.py",
    "/NFL",
    "/NDL",
    "/NJH",
    "/NJS"
)

& robocopy @robocopyArgs | Out-Null
if ($LASTEXITCODE -gt 7) {
    throw "Falha ao copiar app para staging. Robocopy exit code: $LASTEXITCODE"
}

$frontendDist = Join-Path $resolvedBackend "frontend_dist"
if (Test-Path $frontendDist) {
    Copy-Item -Path (Join-Path $frontendDist "*") -Destination (Join-Path $StagingRoot "frontend") -Recurse -Force
}

Copy-Item -LiteralPath (Join-Path $PSScriptRoot "sysvarhub.env.example") -Destination (Join-Path $StagingRoot "config-template\sysvarhub.env.example")
Copy-Item -Path (Join-Path $PSScriptRoot "*.ps1") -Destination (Join-Path $StagingRoot "scripts") -Force

Write-Host "Staging criado em $StagingRoot."
