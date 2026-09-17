param(
    [string]$BackendRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$FrontendRoot = "C:\SysvarHub\Frontend",
    [Parameter(Mandatory = $true)][string]$MySqlRoot,
    [string]$InnoSetupCompiler = ""
)

$ErrorActionPreference = "Stop"

$BuildRoot = Join-Path $PSScriptRoot "build"
$DistRoot = Join-Path $PSScriptRoot "dist"
$StagingRoot = Join-Path $PSScriptRoot "staging"
$RuntimeBuildRoot = Join-Path $BuildRoot "pyinstaller"
$VenvRoot = Join-Path $BuildRoot "venv"
$FrontendBuildRoot = Join-Path $BuildRoot "frontend-src"

function Resolve-Iscc {
    param([string]$ExplicitPath)
    $candidates = @()
    if ($ExplicitPath) { $candidates += $ExplicitPath }
    $candidates += @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return (Resolve-Path $candidate).Path }
    }
    throw "Inno Setup nao encontrado."
}

function Assert-MySqlRoot {
    param([string]$Root)
    if (-not $Root -or -not (Test-Path (Join-Path $Root "bin\mysqld.exe")) -or -not (Test-Path (Join-Path $Root "bin\mysql.exe"))) {
        throw "MySqlRoot invalido. Informe uma distribuicao oficial extraida contendo bin\mysqld.exe e bin\mysql.exe."
    }
    return (Resolve-Path $Root).Path
}

$ResolvedBackend = (Resolve-Path $BackendRoot).Path
$ResolvedFrontend = (Resolve-Path $FrontendRoot).Path
$ResolvedMySql = Assert-MySqlRoot $MySqlRoot
$Iscc = Resolve-Iscc $InnoSetupCompiler

foreach ($path in @($BuildRoot, $DistRoot, $StagingRoot)) {
    if (Test-Path $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
New-Item -ItemType Directory -Force -Path $BuildRoot, $DistRoot, $StagingRoot | Out-Null

& robocopy $ResolvedFrontend $FrontendBuildRoot /E /XD .git node_modules dist .angular /XF .env /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -gt 7) { throw "Falha ao preparar copia temporaria do frontend. Robocopy exit code: $LASTEXITCODE" }

& (Join-Path $PSScriptRoot "build-frontend.ps1") -FrontendRoot $FrontendBuildRoot -BackendRoot $ResolvedBackend

py -3.11 -m venv $VenvRoot
$Python = Join-Path $VenvRoot "Scripts\python.exe"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r (Join-Path $ResolvedBackend "requirements.txt")
& $Python -m pip install -r (Join-Path $PSScriptRoot "requirements-build.txt")

Push-Location (Join-Path $ResolvedBackend "runtime")
try {
    & $Python -m PyInstaller --noconfirm --clean --distpath (Join-Path $RuntimeBuildRoot "dist") --workpath (Join-Path $RuntimeBuildRoot "work") SysvarHubService.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller falhou." }
} finally {
    Pop-Location
}

$RuntimeOutput = Join-Path $RuntimeBuildRoot "dist\SysvarHubService"
if (-not (Test-Path (Join-Path $RuntimeOutput "SysvarHubService.exe"))) {
    throw "Runtime PyInstaller nao foi produzido."
}

New-Item -ItemType Directory -Force -Path `
    (Join-Path $StagingRoot "runtime"), `
    (Join-Path $StagingRoot "frontend"), `
    (Join-Path $StagingRoot "mysql"), `
    (Join-Path $StagingRoot "scripts"), `
    (Join-Path $StagingRoot "config-template") | Out-Null

Copy-Item -Path (Join-Path $RuntimeOutput "*") -Destination (Join-Path $StagingRoot "runtime") -Recurse -Force
Copy-Item -Path (Join-Path $ResolvedBackend "frontend_dist\*") -Destination (Join-Path $StagingRoot "frontend") -Recurse -Force
Copy-Item -Path (Join-Path $ResolvedMySql "*") -Destination (Join-Path $StagingRoot "mysql") -Recurse -Force
Copy-Item -Path (Join-Path $PSScriptRoot "*.ps1") -Destination (Join-Path $StagingRoot "scripts") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "sysvarhub.env.example") -Destination (Join-Path $StagingRoot "config-template\sysvarhub.env.example")

if (-not (Test-Path (Join-Path $StagingRoot "mysql\bin\mysqld.exe"))) { throw "mysqld.exe ausente no staging." }
if (-not (Test-Path (Join-Path $StagingRoot "mysql\bin\mysql.exe"))) { throw "mysql.exe ausente no staging." }
if (-not (Test-Path (Join-Path $StagingRoot "frontend\index.html"))) { throw "Frontend ausente no staging." }

& $Iscc (Join-Path $PSScriptRoot "installer\SysvarHubSetup.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup falhou." }

$Setup = Join-Path $DistRoot "SysvarHubSetup.exe"
if (-not (Test-Path $Setup)) { throw "SysvarHubSetup.exe nao foi gerado." }

$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $Setup
"$($hash.Hash)  SysvarHubSetup.exe" | Set-Content -LiteralPath "$Setup.sha256" -Encoding ASCII

Write-Host "Instalador gerado em $Setup."
