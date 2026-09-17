param(
    [string]$BackendRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$EnvFile = "C:\ProgramData\SysvarHub\config\sysvarhub.env",
    [string]$PythonExe = "py",
    [string]$PythonArgs = "-3.11",
    [switch]$Wait
)

$ErrorActionPreference = "Stop"

if (Test-Path $EnvFile) {
    Get-Content -LiteralPath $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $name, $value = $line.Split("=", 2)
            [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
        }
    }
}

$runtime = Join-Path $BackendRoot "runtime\run_hub.py"

if ($Wait) {
    & $PythonExe $PythonArgs $runtime
    exit $LASTEXITCODE
}

Start-Process -FilePath $PythonExe -ArgumentList "$PythonArgs `"$runtime`"" -WorkingDirectory $BackendRoot -WindowStyle Hidden
