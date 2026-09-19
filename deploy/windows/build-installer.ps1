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

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse("127.0.0.1"), 0)
    try {
        $listener.Start()
        return $listener.LocalEndpoint.Port
    } finally {
        $listener.Stop()
    }
}

function New-RuntimeSmokeProgramData {
    param(
        [string]$Root,
        [string]$FrontendRoot,
        [int]$Port
    )

    $ConfigRoot = Join-Path $Root "config"
    New-Item -ItemType Directory -Force -Path $ConfigRoot, (Join-Path $Root "logs"), (Join-Path $Root "data") | Out-Null

    $EnvFile = Join-Path $ConfigRoot "sysvarhub.env"
    $EnvLines = @(
        "DJANGO_SECRET_KEY=build-smoke-$([guid]::NewGuid().ToString('N'))",
        "DJANGO_DEBUG=False",
        "DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost",
        "DJANGO_CSRF_TRUSTED_ORIGINS=http://127.0.0.1:$Port,http://localhost:$Port",
        "CORS_ALLOWED_ORIGINS=http://127.0.0.1:$Port,http://localhost:$Port",
        "DB_NAME=sysvarhub_db",
        "DB_USER=sysvarhub",
        "DB_PASSWORD=build-smoke-password",
        "DB_HOST=127.0.0.1",
        "DB_PORT=3307",
        "HUB_BIND_HOST=127.0.0.1",
        "HUB_PORT=$Port",
        "SYSVARHUB_LOG_DIR=$(Join-Path $Root 'logs')",
        "SYSVARHUB_DATA_DIR=$(Join-Path $Root 'data')",
        "SYSVARHUB_FRONTEND_DIST_DIR=$FrontendRoot"
    )
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($EnvFile, $EnvLines, $Utf8NoBom)
}

function Invoke-RuntimeGate {
    param(
        [string]$RuntimeRoot,
        [string]$FrontendRoot
    )

    $RuntimeExe = Join-Path $RuntimeRoot "SysvarHubService.exe"
    if (-not (Test-Path $RuntimeExe)) { throw "SysvarHubService.exe ausente no staging." }

    $SmokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("sysvarhub-runtime-smoke-" + [guid]::NewGuid().ToString("N"))
    $Port = Get-FreeTcpPort
    $PreviousProgramData = $env:SYSVARHUB_PROGRAMDATA
    $Process = $null

    try {
        New-RuntimeSmokeProgramData -Root $SmokeRoot -FrontendRoot $FrontendRoot -Port $Port
        $env:SYSVARHUB_PROGRAMDATA = $SmokeRoot

        & $RuntimeExe manage check
        if ($LASTEXITCODE -ne 0) { throw "Gate runtime falhou em manage check empacotado." }
        Write-Host "Gate runtime: manage check empacotado OK."

        $ImportCheck = @"
from django.conf import settings
from django.utils.module_loading import import_string
import importlib

imports = [
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'whitenoise.storage.CompressedManifestStaticFilesStorage',
    'corsheaders.middleware.CorsMiddleware',
    'django_filters.rest_framework.DjangoFilterBackend',
    'rest_framework.authentication.TokenAuthentication',
    'rest_framework.authentication.SessionAuthentication',
    'rest_framework.permissions.IsAuthenticated',
    'rest_framework.pagination.PageNumberPagination',
]
for dotted_path in imports:
    import_string(dotted_path)
import_string(settings.STATICFILES_STORAGE)
importlib.import_module(settings.DATABASES['default']['ENGINE'] + '.base')
print('dynamic imports ok')
"@
        & $RuntimeExe manage shell -c $ImportCheck
        if ($LASTEXITCODE -ne 0) { throw "Gate runtime falhou na auditoria de imports dinamicos empacotados." }
        Write-Host "Gate runtime: imports dinamicos empacotados OK."

        $StartInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $StartInfo.FileName = $RuntimeExe
        $StartInfo.ArgumentList.Add("console")
        $StartInfo.UseShellExecute = $false
        $StartInfo.RedirectStandardOutput = $true
        $StartInfo.RedirectStandardError = $true
        $StartInfo.Environment["SYSVARHUB_PROGRAMDATA"] = $SmokeRoot
        $Process = [System.Diagnostics.Process]::Start($StartInfo)

        $Deadline = (Get-Date).AddSeconds(45)
        $HealthResponse = $null
        while ((Get-Date) -lt $Deadline) {
            if ($Process.HasExited) {
                $stdout = $Process.StandardOutput.ReadToEnd()
                $stderr = $Process.StandardError.ReadToEnd()
                throw "Gate runtime falhou: console encerrou antes do health check. stdout=$stdout stderr=$stderr"
            }
            try {
                $HealthResponse = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health/" -TimeoutSec 2
                break
            } catch {
                Start-Sleep -Milliseconds 500
            }
        }

        if ($null -eq $HealthResponse) { throw "Gate runtime falhou: timeout aguardando /api/health/." }
        if ($HealthResponse.status -ne "ok" -or $HealthResponse.service -ne "sysvar-hub") {
            throw "Gate runtime falhou: payload invalido em /api/health/."
        }
        Write-Host "Gate runtime: /api/health/ OK."

        $IndexResponse = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/" -TimeoutSec 5
        if ($IndexResponse.StatusCode -ne 200) { throw "Gate runtime falhou: GET / retornou HTTP $($IndexResponse.StatusCode)." }
        Write-Host "Gate runtime: GET / OK."
    } finally {
        if ($Process -and -not $Process.HasExited) {
            $Process.Kill()
            $Process.WaitForExit()
        }
        if ($null -eq $PreviousProgramData) {
            Remove-Item Env:\SYSVARHUB_PROGRAMDATA -ErrorAction SilentlyContinue
        } else {
            $env:SYSVARHUB_PROGRAMDATA = $PreviousProgramData
        }
        Remove-Item -LiteralPath $SmokeRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
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

Invoke-RuntimeGate -RuntimeRoot (Join-Path $StagingRoot "runtime") -FrontendRoot (Join-Path $StagingRoot "frontend")

& $Iscc (Join-Path $PSScriptRoot "installer\SysvarHubSetup.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup falhou." }

$Setup = Join-Path $DistRoot "SysvarHubSetup.exe"
if (-not (Test-Path $Setup)) { throw "SysvarHubSetup.exe nao foi gerado." }

$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $Setup
"$($hash.Hash)  SysvarHubSetup.exe" | Set-Content -LiteralPath "$Setup.sha256" -Encoding ASCII

Write-Host "Instalador gerado em $Setup."
