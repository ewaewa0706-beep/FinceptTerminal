[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$qtRoot = Split-Path -Parent $scriptRoot
$repoRoot = Split-Path -Parent $qtRoot
$cmakeFile = Join-Path $qtRoot "CMakeLists.txt"

function Get-ExecutablePath {
    param([Parameter(Mandatory = $true)][string[]]$Names)

    foreach ($name in $Names) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command) {
            return $command.Source
        }
    }
    return $null
}

function Get-FirstExistingPath {
    param([string[]]$Candidates)

    foreach ($candidate in $Candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Get-QtToolPath {
    param([Parameter(Mandatory = $true)][string]$ToolName)

    $fromPath = Get-ExecutablePath @($ToolName, "$ToolName.exe")
    if ($null -ne $fromPath) {
        return $fromPath
    }

    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:QT_DIR)) {
        $candidates += Join-Path (Join-Path $env:QT_DIR "bin") "$ToolName.exe"
    }
    if (-not [string]::IsNullOrWhiteSpace($env:QT_ROOT_DIR)) {
        $candidates += Join-Path (Join-Path $env:QT_ROOT_DIR "bin") "$ToolName.exe"
    }

    $qtBases = @("C:\Qt")
    if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        $qtBases += Join-Path $env:USERPROFILE "Qt"
    }
    foreach ($base in $qtBases) {
        if (-not (Test-Path -LiteralPath $base -PathType Container)) {
            continue
        }
        $matches = Get-ChildItem -Path (Join-Path $base "6.8.*\msvc*_64\bin\$ToolName.exe") -File -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending
        foreach ($match in $matches) {
            $candidates += $match.FullName
        }
    }

    return Get-FirstExistingPath $candidates
}

function Invoke-VersionCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Arguments = @()
    )

    try {
        $output = & $Executable @Arguments 2>&1 | Out-String
        return $output.Trim()
    }
    catch {
        return ""
    }
}

function Add-Check {
    param(
        [Parameter(Mandatory = $true)][string]$Component,
        [Parameter(Mandatory = $true)][bool]$Required,
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$Version = "",
        [string]$Location = "",
        [string]$Detail = ""
    )

    $script:checks += [PSCustomObject]@{
        Component = $Component
        Required = if ($Required) { "yes" } else { "no" }
        Status = $Status
        Version = $Version
        Location = $Location
        Detail = $Detail
    }
}

if ($env:OS -ne "Windows_NT") {
    Write-Error "windows_dev_doctor.ps1 is intended for Windows hosts."
    exit 2
}

$cmakeText = Get-Content -LiteralPath $cmakeFile -Raw
$cmakeMinimum = "3.27"
if ($cmakeText -match 'cmake_minimum_required\(VERSION\s+([0-9.]+)\)') {
    $cmakeMinimum = $Matches[1]
}
$qtMinor = "6.8"
if ($cmakeText -match 'set\(FINCEPT_QT_VERSION_MINOR\s+([0-9.]+)\)') {
    $qtMinor = $Matches[1]
}
$msvcMinimum = 1940
if ($cmakeText -match 'MSVC_VERSION\s+LESS\s+([0-9]+)') {
    $msvcMinimum = [int]$Matches[1]
}

$checks = @()

$cmake = Get-ExecutablePath @("cmake", "cmake.exe")
if ($null -eq $cmake) {
    Add-Check "CMake" $true "MISSING" "" "" "Requires >= $cmakeMinimum"
}
else {
    $raw = Invoke-VersionCommand $cmake @("--version")
    $version = if ($raw -match 'cmake version\s+([0-9.]+)') { $Matches[1] } else { "unknown" }
    $ok = $version -ne "unknown" -and ([version]$version -ge [version]$cmakeMinimum)
    Add-Check "CMake" $true $(if ($ok) { "OK" } else { "FAIL" }) $version $cmake "Requires >= $cmakeMinimum"
}

$ninja = Get-ExecutablePath @("ninja", "ninja.exe")
if ($null -eq $ninja) {
    Add-Check "Ninja" $true "MISSING" "" "" "Required by CMakePresets.json"
}
else {
    $version = (Invoke-VersionCommand $ninja @("--version")).Split([Environment]::NewLine)[0].Trim()
    Add-Check "Ninja" $true "OK" $version $ninja "Generator used by all checked-in presets"
}

$cl = Get-ExecutablePath @("cl", "cl.exe")
if ($null -eq $cl) {
    Add-Check "MSVC cl" $true "MISSING" "" "" "Open a VS 2022 Developer PowerShell; requires MSVC_VERSION >= $msvcMinimum"
}
else {
    $raw = Invoke-VersionCommand $cl @()
    $version = if ($raw -match 'Version\s+([0-9.]+)') { $Matches[1] } else { "unknown" }
    $numeric = 0
    if ($version -match '^([0-9]+)\.([0-9]+)') {
        $numeric = ([int]$Matches[1] * 100) + [int]$Matches[2]
    }
    $ok = $numeric -ge $msvcMinimum
    Add-Check "MSVC cl" $true $(if ($ok) { "OK" } else { "FAIL" }) $version $cl "Requires MSVC_VERSION >= $msvcMinimum"
}

$vswhere = Get-FirstExistingPath @(
    (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe")
)
if ($null -ne $vswhere) {
    $install = Invoke-VersionCommand $vswhere @("-latest", "-products", "*", "-property", "installationPath")
    if (-not [string]::IsNullOrWhiteSpace($install)) {
        $msbuildFromVs = Join-Path $install "MSBuild\Current\Bin\MSBuild.exe"
    }
}
$msbuild = Get-ExecutablePath @("msbuild", "msbuild.exe")
if ($null -eq $msbuild -and $null -ne $msbuildFromVs -and (Test-Path -LiteralPath $msbuildFromVs -PathType Leaf)) {
    $msbuild = $msbuildFromVs
}
if ($null -eq $msbuild) {
    Add-Check "MSBuild" $false "MISSING" "" "" "Optional with the Ninja presets"
}
else {
    $raw = Invoke-VersionCommand $msbuild @("-version", "-nologo")
    $version = ($raw -split "`r?`n" | Where-Object { $_ -match '^[0-9]+\.[0-9.]+' } | Select-Object -Last 1)
    Add-Check "MSBuild" $false "OK" $version $msbuild "Optional with the Ninja presets"
}

$python = Get-ExecutablePath @("python", "python.exe")
$pythonReady = $false
if ($null -ne $python) {
    $raw = Invoke-VersionCommand $python @("--version")
    $version = if ($raw -match 'Python\s+([0-9.]+)') { $Matches[1] } else { "unknown" }
    $pythonReady = $version -match '^3\.11(\.|$)'
}
if (-not $pythonReady) {
    $pyLauncher = Get-ExecutablePath @("py", "py.exe")
    if ($null -ne $pyLauncher) {
        $launcherRaw = Invoke-VersionCommand $pyLauncher @("-3.11", "--version")
        if ($launcherRaw -match 'Python\s+(3\.11(?:\.[0-9]+)?)') {
            $version = $Matches[1]
            $python = "$pyLauncher -3.11"
            $pythonReady = $true
        }
    }
}
if ($pythonReady) {
    Add-Check "Python" $true "OK" $version $python "Expected Python 3.11.x"
}
elseif ($null -eq $python) {
    Add-Check "Python" $true "MISSING" "" "" "Personal KR validation targets Python 3.11"
}
else {
    Add-Check "Python" $true "FAIL" $version $python "Expected Python 3.11.x (PATH python or py -3.11)"
}

$qmake = Get-QtToolPath "qmake"
if ($null -eq $qmake) {
    Add-Check "Qt qmake" $true "MISSING" "" "" "Expected Qt $qtMinor.x MSVC kit"
}
else {
    $version = Invoke-VersionCommand $qmake @("-query", "QT_VERSION")
    $ok = $version -match ("^" + [regex]::Escape($qtMinor) + "(\.|$)")
    Add-Check "Qt qmake" $true $(if ($ok) { "OK" } else { "FAIL" }) $version $qmake "Expected Qt $qtMinor.x"
}

$windeployqt = Get-QtToolPath "windeployqt"
if ($null -eq $windeployqt) {
    Add-Check "windeployqt" $true "MISSING" "" "" "Used by Windows post-build deployment"
}
else {
    $raw = Invoke-VersionCommand $windeployqt @("--version")
    $version = if ($raw -match '([0-9]+\.[0-9]+\.[0-9]+)') { $Matches[1] } else { $raw }
    Add-Check "windeployqt" $true "OK" $version $windeployqt "Used by Windows post-build deployment"
}

$vcpkgCandidates = @()
if (-not [string]::IsNullOrWhiteSpace($env:VCPKG_ROOT)) {
    $vcpkgCandidates += Join-Path $env:VCPKG_ROOT "vcpkg.exe"
}
$vcpkg = Get-ExecutablePath @("vcpkg", "vcpkg.exe")
if ($null -eq $vcpkg) {
    $vcpkg = Get-FirstExistingPath $vcpkgCandidates
}
if ($null -eq $vcpkg) {
    Add-Check "vcpkg" $false "MISSING" "" "" "Optional; CMake can use it to locate Windows OpenSSL"
}
else {
    $raw = Invoke-VersionCommand $vcpkg @("version")
    $version = ($raw -split "`r?`n" | Select-Object -First 1).Trim()
    Add-Check "vcpkg" $false "OK" $version $vcpkg "Optional OpenSSL source"
}

$sdkRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\Include"
$sdkVersion = ""
$sdkPath = ""
if (Test-Path -LiteralPath $sdkRoot -PathType Container) {
    $sdk = Get-ChildItem -LiteralPath $sdkRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "um\windows.h") -PathType Leaf } |
        Sort-Object { try { [version]$_.Name } catch { [version]"0.0" } } -Descending |
        Select-Object -First 1
    if ($null -ne $sdk) {
        $sdkVersion = $sdk.Name
        $sdkPath = $sdk.FullName
    }
}
if ([string]::IsNullOrWhiteSpace($sdkPath)) {
    Add-Check "Windows SDK" $true "MISSING" "" "" "Install the Windows 10/11 SDK with Visual Studio C++ workload"
}
else {
    Add-Check "Windows SDK" $true "OK" $sdkVersion $sdkPath "Required for Windows headers/resources"
}

$opensslCandidates = @()
if (-not [string]::IsNullOrWhiteSpace($env:OPENSSL_ROOT_DIR)) {
    $opensslCandidates += $env:OPENSSL_ROOT_DIR
}
if (-not [string]::IsNullOrWhiteSpace($env:VCPKG_INSTALLED_DIR)) {
    $opensslCandidates += Join-Path $env:VCPKG_INSTALLED_DIR "x64-windows"
}
if (-not [string]::IsNullOrWhiteSpace($env:VCPKG_ROOT)) {
    $opensslCandidates += Join-Path (Join-Path $env:VCPKG_ROOT "installed") "x64-windows"
}
$opensslCandidates += "C:\windowsdisk\fincept-cpp\vcpkg_installed\x64-windows"
$opensslRoot = $null
foreach ($candidate in $opensslCandidates) {
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        continue
    }
    $crypto = Join-Path $candidate "lib\libcrypto.lib"
    $sslHeader = Join-Path $candidate "include\openssl\ssl.h"
    if ((Test-Path -LiteralPath $crypto -PathType Leaf) -and (Test-Path -LiteralPath $sslHeader -PathType Leaf)) {
        $opensslRoot = (Resolve-Path -LiteralPath $candidate).Path
        break
    }
}
if ($null -eq $opensslRoot) {
    Add-Check "OpenSSL" $true "MISSING" "" "" "CMakeLists.txt uses find_package(OpenSSL REQUIRED); set OPENSSL_ROOT_DIR or install x64-windows OpenSSL via vcpkg"
}
else {
    Add-Check "OpenSSL" $true "OK" "detected" $opensslRoot "CMake OpenSSL root candidate"
}

Write-Host "Fincept Windows development doctor"
Write-Host "Repository: $repoRoot"
Write-Host "Checks are read-only; this script does not configure, build, install, or modify files."
try {
    $git = Get-ExecutablePath @("git", "git.exe")
    if ($null -ne $git) {
        $branch = (& $git -C $repoRoot branch --show-current 2>$null | Out-String).Trim()
        $head = (& $git -C $repoRoot rev-parse --short HEAD 2>$null | Out-String).Trim()
        if (-not [string]::IsNullOrWhiteSpace($branch)) {
            Write-Host "Git: $branch @ $head"
        }
    }
}
catch {
    Write-Host "Git: unavailable for repository metadata"
}
Write-Host ""
$checks | Format-Table -AutoSize Component, Required, Status, Version, Location

$buildRoot = Join-Path $qtRoot "build"
Write-Host ""
Write-Host "Existing build state"
if (-not (Test-Path -LiteralPath $buildRoot -PathType Container)) {
    Write-Host "  build/: absent"
}
else {
    $buildDirs = Get-ChildItem -LiteralPath $buildRoot -Directory -ErrorAction SilentlyContinue
    if ($buildDirs.Count -eq 0) {
        Write-Host "  build/: present, no preset directories"
    }
    foreach ($dir in $buildDirs) {
        $cache = Join-Path $dir.FullName "CMakeCache.txt"
        $cacheState = if (Test-Path -LiteralPath $cache -PathType Leaf) { "CMakeCache.txt present" } else { "no CMakeCache.txt" }
        Write-Host "  $($dir.Name): $cacheState"
    }
}

Write-Host ""
if ($null -ne $vswhere) {
    Write-Host "Visual Studio locator: $vswhere"
}
if (-not [string]::IsNullOrWhiteSpace($env:QT_DIR)) {
    Write-Host "QT_DIR is set and was considered for Qt tool discovery."
}
if (-not [string]::IsNullOrWhiteSpace($env:VCPKG_ROOT)) {
    Write-Host "VCPKG_ROOT is set and was considered for vcpkg discovery."
}

$failedRequired = @($checks | Where-Object { $_.Required -eq "yes" -and $_.Status -ne "OK" })
if ($failedRequired.Count -gt 0) {
    Write-Host ""
    Write-Host "Current shell is not ready for the checked-in Windows Ninja presets."
    foreach ($failure in $failedRequired) {
        Write-Host "  - $($failure.Component): $($failure.Status). $($failure.Detail)"
    }
    exit 1
}

Write-Host ""
Write-Host "Current shell satisfies the checked Windows development prerequisites."
exit 0
