[CmdletBinding()]
param(
    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$DevRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals((Split-Path -Leaf $DevRoot), "market-ai-dev")) {
    throw "Clean-dev helper must run from a repository directory named market-ai-dev. Resolved: $DevRoot"
}

# These are generated/runtime artifacts only. Mutable developer data under db/ is
# intentionally outside this list and is never deleted by this helper.
$RootFiles = @(
    "MarketAI.exe",
    "InvestmentLocalSuite.exe",
    "KisKospi200Bridge.exe",
    "KisKospi200Bridge.exe.config",
    "AxInterop.ITGExpertCtlLib.dll",
    "Interop.ITGExpertCtlLib.dll"
)
$RootDirectories = @("_internal", "_suite_internal", "_runtime-backup", ".pytest_cache")
$BridgeBuildDirectories = @(
    (Join-Path $DevRoot "KisKospi200Bridge\bin"),
    (Join-Path $DevRoot "KisKospi200Bridge\obj")
)

function Get-GeneratedArtifacts {
    $items = New-Object System.Collections.Generic.List[string]
    foreach ($name in $RootFiles) {
        $path = Join-Path $DevRoot $name
        if (Test-Path -LiteralPath $path -PathType Leaf) { $items.Add($path) }
    }
    foreach ($name in $RootDirectories) {
        $path = Join-Path $DevRoot $name
        if (Test-Path -LiteralPath $path -PathType Container) { $items.Add($path) }
    }
    foreach ($path in $BridgeBuildDirectories) {
        if (Test-Path -LiteralPath $path -PathType Container) { $items.Add($path) }
    }
    foreach ($directory in @(Get-ChildItem -LiteralPath $DevRoot -Directory -Recurse -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq "__pycache__" })) {
        $items.Add($directory.FullName)
    }
    foreach ($file in @(Get-ChildItem -LiteralPath $DevRoot -File -Recurse -Force -ErrorAction SilentlyContinue | Where-Object { $_.Extension -in @(".pyc", ".pyo") })) {
        $items.Add($file.FullName)
    }
    return @($items | Sort-Object -Unique)
}

$before = @(Get-GeneratedArtifacts)
if ($VerifyOnly) {
    if ($before.Count -gt 0) {
        Write-Host "[CLEAN-DEV] FAIL - generated/runtime artifacts remain:" -ForegroundColor Red
        foreach ($path in $before) { Write-Host "  - $path" }
        exit 1
    }
    Write-Host "[CLEAN-DEV] PASS - no generated/runtime artifacts in market-ai-dev." -ForegroundColor Green
    exit 0
}

foreach ($path in $before) {
    if (Test-Path -LiteralPath $path -PathType Container) {
        Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop
    } elseif (Test-Path -LiteralPath $path -PathType Leaf) {
        Remove-Item -LiteralPath $path -Force -ErrorAction Stop
    }
}

$after = @(Get-GeneratedArtifacts)
if ($after.Count -gt 0) {
    Write-Host "[CLEAN-DEV] FAIL - cleanup incomplete:" -ForegroundColor Red
    foreach ($path in $after) { Write-Host "  - $path" }
    exit 1
}

Write-Host "[CLEAN-DEV] PASS - generated/runtime artifacts removed; db/ developer data preserved." -ForegroundColor Green
exit 0
