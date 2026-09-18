[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$credentialNames = @(
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "KRX_AUTH_KEY",
    "DART_API_KEY",
    "NAVER_CLIENT_ID",
    "NAVER_CLIENT_SECRET",
    "ECOS_API_KEY",
    "GOOGLE_API_KEY"
)
$configurationNames = @("KRX_AUTH_KEY_FILE")
$savedEnvironment = @{}

function Invoke-PythonChecked {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code ${LASTEXITCODE}: $Python $($Arguments -join ' ')"
    }
}

function Invoke-PythonStdinChecked {
    param([Parameter(Mandatory = $true)][string]$Code)

    $Code | & $Python -
    if ($LASTEXITCODE -ne 0) {
        throw "Python stdin command failed with exit code $LASTEXITCODE"
    }
}

Push-Location $scriptRoot
try {
    foreach ($name in $credentialNames) {
        $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    foreach ($name in $configurationNames) {
        $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }
    # Prevent a developer's git-ignored KRX key file from changing deterministic
    # test/status behavior or causing validation to make authenticated calls.
    [Environment]::SetEnvironmentVariable("KRX_AUTH_KEY_FILE", "__disabled_for_validation__", "Process")
    $savedEnvironment["PYTHONDONTWRITEBYTECODE"] = [Environment]::GetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "Process")
    $savedEnvironment["PYTHONUTF8"] = [Environment]::GetEnvironmentVariable("PYTHONUTF8", "Process")
    [Environment]::SetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "1", "Process")
    [Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")

    Write-Host "Personal KR: syntax compile (no bytecode writes)"
    $syntaxCheck = @'
from pathlib import Path

paths = [Path("personal_kr_terminal.py"), *sorted(Path("personal_kr").rglob("*.py"))]
for path in paths:
    compile(path.read_bytes(), str(path), "exec")
print(f"syntax OK: {len(paths)} Python files")
'@
    Invoke-PythonStdinChecked $syntaxCheck

    Write-Host "Personal KR: unittest regression suite"
    Invoke-PythonChecked @("-m", "unittest", "discover", "-s", "personal_kr/tests", "-v")

    Write-Host "Personal KR: headless status contract"
    Invoke-PythonChecked @("personal_kr_terminal.py", "status")

    Write-Host "Personal KR validation passed."
}
finally {
    Pop-Location
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
    }
}
