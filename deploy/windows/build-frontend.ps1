param(
    [string]$FrontendRoot = "C:\SysvarHub\Frontend",
    [string]$BackendRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $FrontendRoot)) {
    throw "FrontendRoot nao encontrado: $FrontendRoot"
}

Push-Location $FrontendRoot
try {
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm -and (Test-Path "C:\Program Files\nodejs\npm.cmd")) {
        $npm = Get-Item "C:\Program Files\nodejs\npm.cmd"
    }
    if (-not $npm) {
        $npm = Get-Command npm -ErrorAction Stop
    }
    $npx = Get-Command npx.cmd -ErrorAction SilentlyContinue
    if (-not $npx -and (Test-Path "C:\Program Files\nodejs\npx.cmd")) {
        $npx = Get-Item "C:\Program Files\nodejs\npx.cmd"
    }
    if (-not $npx) {
        $npx = Get-Command npx -ErrorAction Stop
    }

    $npmPath = if ($npm.Source) { $npm.Source } else { $npm.FullName }
    $npxPath = if ($npx.Source) { $npx.Source } else { $npx.FullName }

    & $npmPath ci
    if ($LASTEXITCODE -ne 0) {
        throw "npm ci falhou com exit code $LASTEXITCODE"
    }
    & $npxPath ng build
    if ($LASTEXITCODE -ne 0) {
        throw "ng build falhou com exit code $LASTEXITCODE"
    }

    $distRoot = Join-Path $FrontendRoot "dist"
    $index = Get-ChildItem -Path $distRoot -Filter "index.html" -Recurse -File |
        Sort-Object { $_.FullName.Length } |
        Select-Object -First 1

    if (-not $index) {
        throw "index.html nao encontrado em $distRoot"
    }

    $source = $index.Directory.FullName
    $target = Join-Path $BackendRoot "frontend_dist"

    if ((Resolve-Path $target -ErrorAction SilentlyContinue) -and
        ((Resolve-Path $target).Path -ne (Join-Path (Resolve-Path $BackendRoot).Path "frontend_dist"))) {
        throw "Diretorio frontend_dist inesperado: $target"
    }

    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Get-ChildItem -LiteralPath $target -Force | Remove-Item -Recurse -Force
    Copy-Item -Path (Join-Path $source "*") -Destination $target -Recurse -Force
    New-Item -ItemType File -Force -Path (Join-Path $target ".gitkeep") | Out-Null

    Write-Host "Frontend copiado para $target."
} finally {
    Pop-Location
}
