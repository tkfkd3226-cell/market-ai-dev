[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("MarketAI", "InvestmentLocalSuite", "KisBridge")]
    [string]$Component,

    [Parameter(Mandatory = $true)]
    [string]$StagingRoot,

    [switch]$PlanOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$DeployRevision = "phase1-sibling-runtime-deploy-r1"
$DevRoot = Split-Path -Parent $PSScriptRoot
$ParentRoot = Split-Path -Parent $DevRoot
$RuntimeRoot = Join-Path $ParentRoot "market-ai"
$RollbackBase = Join-Path $env:TEMP "InvestmentLocalSuite\RuntimeDeployRollback"

function Write-DeployStep([string]$Message) {
    Write-Host "[RUNTIME-DEPLOY] $Message"
}

function Fail([string]$Message) {
    throw $Message
}

function Get-NormalizedFullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd([char[]]@('\', '/'))
}

function Test-PathEquals([string]$Left, [string]$Right) {
    return [System.StringComparer]::OrdinalIgnoreCase.Equals(
        (Get-NormalizedFullPath $Left),
        (Get-NormalizedFullPath $Right)
    )
}

function Test-PathInside([string]$Child, [string]$Parent) {
    $childPath = Get-NormalizedFullPath $Child
    $parentPath = Get-NormalizedFullPath $Parent
    if ([System.StringComparer]::OrdinalIgnoreCase.Equals($childPath, $parentPath)) {
        return $true
    }
    $prefix = $parentPath + [System.IO.Path]::DirectorySeparatorChar
    return $childPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Require-Directory([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        Fail "$Label directory not found: $Path"
    }
}

function Require-File([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Fail "$Label file not found: $Path"
    }
}

function Get-ComponentSpec([string]$Name) {
    switch ($Name) {
        "MarketAI" {
            return @{
                DisplayName = "Market AI"
                Files = @("MarketAI.exe")
                Directories = @("_internal")
                CopyOrder = @("_internal", "MarketAI.exe")
                Executable = "MarketAI.exe"
            }
        }
        "InvestmentLocalSuite" {
            return @{
                DisplayName = "Investment Local Suite"
                Files = @("InvestmentLocalSuite.exe")
                Directories = @("_suite_internal")
                CopyOrder = @("_suite_internal", "InvestmentLocalSuite.exe")
                Executable = "InvestmentLocalSuite.exe"
            }
        }
        "KisBridge" {
            return @{
                DisplayName = "KIS eFriend Market Bridge"
                Files = @(
                    "AxInterop.ITGExpertCtlLib.dll",
                    "Interop.ITGExpertCtlLib.dll",
                    "KisKospi200Bridge.exe.config",
                    "KisKospi200Bridge.exe"
                )
                Directories = @()
                CopyOrder = @(
                    "AxInterop.ITGExpertCtlLib.dll",
                    "Interop.ITGExpertCtlLib.dll",
                    "KisKospi200Bridge.exe.config",
                    "KisKospi200Bridge.exe"
                )
                Executable = "KisKospi200Bridge.exe"
            }
        }
        default {
            Fail "Unsupported runtime component: $Name"
        }
    }
}

function Get-RelativeManifest([string]$Root, [string[]]$Names) {
    $manifest = @{}
    foreach ($name in $Names) {
        $path = Join-Path $Root $name
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
            $manifest[$name.Replace('\', '/')] = $hash
            continue
        }
        if (Test-Path -LiteralPath $path -PathType Container) {
            $files = @(Get-ChildItem -LiteralPath $path -File -Recurse | Sort-Object FullName)
            foreach ($file in $files) {
                $relative = $file.FullName.Substring((Get-NormalizedFullPath $Root).Length).TrimStart([char[]]@('\', '/'))
                $relative = $relative.Replace('\', '/')
                $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
                $manifest[$relative] = $hash
            }
            continue
        }
        Fail "Manifest input is missing: $path"
    }
    return $manifest
}

function Assert-ManifestsEqual($Expected, $Actual, [string]$Label) {
    $expectedKeys = @($Expected.Keys | Sort-Object)
    $actualKeys = @($Actual.Keys | Sort-Object)
    if ($expectedKeys.Count -ne $actualKeys.Count) {
        Fail "$Label manifest file-count mismatch: expected $($expectedKeys.Count), got $($actualKeys.Count)."
    }
    foreach ($key in $expectedKeys) {
        if (-not $Actual.ContainsKey($key)) {
            Fail "$Label manifest is missing: $key"
        }
        if ($Expected[$key] -ne $Actual[$key]) {
            Fail "$Label SHA-256 mismatch: $key"
        }
    }
}

function Copy-PathExact([string]$SourceRoot, [string]$DestinationRoot, [string]$Name) {
    $source = Join-Path $SourceRoot $Name
    $destination = Join-Path $DestinationRoot $Name
    if (Test-Path -LiteralPath $source -PathType Container) {
        Copy-Item -LiteralPath $source -Destination $destination -Recurse -ErrorAction Stop
    } else {
        Copy-Item -LiteralPath $source -Destination $destination -Force -ErrorAction Stop
    }
}

function Remove-PathExact([string]$Root, [string]$Name) {
    $path = Join-Path $Root $Name
    if (-not (Test-Path -LiteralPath $path)) {
        return
    }
    if (Test-Path -LiteralPath $path -PathType Container) {
        Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop
    } else {
        Remove-Item -LiteralPath $path -Force -ErrorAction Stop
    }
}

$devLeaf = Split-Path -Leaf (Get-NormalizedFullPath $DevRoot)
$runtimeLeaf = Split-Path -Leaf (Get-NormalizedFullPath $RuntimeRoot)
if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals($devLeaf, "market-ai-dev")) {
    Fail "Deployment helper must run from a repository directory named market-ai-dev. Resolved: $DevRoot"
}
if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals($runtimeLeaf, "market-ai")) {
    Fail "Runtime target must be the sibling directory named market-ai. Resolved: $RuntimeRoot"
}
if (-not (Test-PathEquals (Split-Path -Parent $DevRoot) (Split-Path -Parent $RuntimeRoot))) {
    Fail "market-ai-dev and market-ai must share the same parent directory."
}
if (Test-PathEquals $DevRoot $RuntimeRoot) {
    Fail "Development and runtime roots must be different directories."
}

Require-Directory $RuntimeRoot "Sibling market-ai runtime"
Require-File (Join-Path $RuntimeRoot "README.md") "Runtime repository marker README.md"
Require-Directory $StagingRoot "Staging"

$StagingRoot = Get-NormalizedFullPath $StagingRoot
if (Test-PathInside $StagingRoot $RuntimeRoot) {
    Fail "Staging root must not be inside the operating market-ai directory: $StagingRoot"
}

$spec = Get-ComponentSpec $Component
$allNames = @($spec.Files) + @($spec.Directories)
foreach ($fileName in @($spec.Files)) {
    Require-File (Join-Path $StagingRoot $fileName) "Staged $fileName"
}
foreach ($directoryName in @($spec.Directories)) {
    Require-Directory (Join-Path $StagingRoot $directoryName) "Staged $directoryName"
}

$stagedManifest = Get-RelativeManifest $StagingRoot $allNames
if ($stagedManifest.Count -eq 0) {
    Fail "Staged runtime manifest is empty for $Component."
}

Write-DeployStep "Revision: $DeployRevision"
Write-DeployStep "Component: $($spec.DisplayName)"
Write-DeployStep "Dev root: $DevRoot"
Write-DeployStep "Runtime target: $RuntimeRoot"
Write-DeployStep "Staging root: $StagingRoot"
Write-DeployStep "Staged manifest: $($stagedManifest.Count) files"

if ($PlanOnly) {
    Write-Host ""
    Write-Host "=============================================================="
    Write-Host " RUNTIME DEPLOY PLAN : PASS" -ForegroundColor Green
    Write-Host "=============================================================="
    Write-Host "Component : $($spec.DisplayName)"
    Write-Host "Target    : $RuntimeRoot"
    Write-Host "Files     : $($stagedManifest.Count) staged files verified"
    Write-Host "Mutation  : NONE (-PlanOnly)"
    return
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$rollbackId = [Guid]::NewGuid().ToString("N")
$RollbackRoot = Join-Path $RollbackBase ("{0}-{1}-{2}" -f $Component, $timestamp, $rollbackId)
$ExistingNames = @()
$BackupVerified = $false

try {
    New-Item -ItemType Directory -Path $RollbackRoot -Force | Out-Null

    foreach ($name in $allNames) {
        $runtimePath = Join-Path $RuntimeRoot $name
        if (Test-Path -LiteralPath $runtimePath) {
            Copy-PathExact $RuntimeRoot $RollbackRoot $name
            $ExistingNames += $name
        }
    }

    if ($ExistingNames.Count -gt 0) {
        $runtimeBefore = Get-RelativeManifest $RuntimeRoot $ExistingNames
        $backupManifest = Get-RelativeManifest $RollbackRoot $ExistingNames
        Assert-ManifestsEqual $runtimeBefore $backupManifest "Rollback backup"
        $BackupVerified = $true
        Write-DeployStep "Rollback backup verified: $RollbackRoot"
    } else {
        $BackupVerified = $true
        Write-DeployStep "No previous runtime files for this component; rollback backup is empty."
    }

    # Clean replacement boundary. Support directories are always removed as whole
    # directories before any new runtime file is copied, so obsolete PyInstaller
    # files can never accumulate across builds.
    foreach ($directoryName in @($spec.Directories)) {
        Remove-PathExact $RuntimeRoot $directoryName
        if (Test-Path -LiteralPath (Join-Path $RuntimeRoot $directoryName)) {
            Fail "Old support directory could not be fully removed: $directoryName"
        }
        Write-DeployStep "Old support directory removed completely: $directoryName"
    }

    # Remove old component files too. The executable is copied last so an
    # incomplete support-directory deployment does not look runnable.
    foreach ($fileName in @($spec.Files)) {
        Remove-PathExact $RuntimeRoot $fileName
    }

    foreach ($name in @($spec.CopyOrder)) {
        Copy-PathExact $StagingRoot $RuntimeRoot $name
    }

    $deployedManifest = Get-RelativeManifest $RuntimeRoot $allNames
    Assert-ManifestsEqual $stagedManifest $deployedManifest "Deployed runtime"

    $rollbackStatus = "NOT NEEDED"
    if (Test-Path -LiteralPath $RollbackRoot) {
        try {
            Remove-Item -LiteralPath $RollbackRoot -Recurse -Force -ErrorAction Stop
            $rollbackStatus = "VERIFIED / TEMP CLEANUP PASS"
        } catch {
            $rollbackStatus = "VERIFIED / TEMP BACKUP RETAINED"
            Write-Host "[WARN] Verified rollback backup could not be removed after success: $RollbackRoot" -ForegroundColor Yellow
        }
    }

    Write-Host ""
    Write-Host "=============================================================="
    Write-Host " RUNTIME DEPLOY : SUCCESS" -ForegroundColor Green
    Write-Host "=============================================================="
    Write-Host "Component     : $($spec.DisplayName)"
    Write-Host "Target        : $RuntimeRoot"
    Write-Host "Clean replace : PASS"
    Write-Host "Hash verify   : PASS ($($deployedManifest.Count) files)"
    Write-Host "Rollback      : $rollbackStatus"
    Write-Host "Executable    : $($spec.Executable)"
    Write-Host "=============================================================="
} catch {
    $DeploymentError = $_
    Write-Host "[ROLLBACK] Runtime deployment failed: $($DeploymentError.Exception.Message)" -ForegroundColor Yellow

    if ($BackupVerified) {
        $clearFailed = $false
        foreach ($name in $allNames) {
            try {
                Remove-PathExact $RuntimeRoot $name
            } catch {
                $clearFailed = $true
                Write-Host "[WARN] Could not clear failed runtime path '$name': $($_.Exception.Message)" -ForegroundColor Yellow
            }
        }

        if (-not $clearFailed) {
            $restoreDirectories = @($spec.Directories | Where-Object { $ExistingNames -contains $_ })
            $restoreFiles = @($spec.Files | Where-Object { $ExistingNames -contains $_ })
            try {
                foreach ($name in $restoreDirectories) {
                    Copy-PathExact $RollbackRoot $RuntimeRoot $name
                }
                foreach ($name in $restoreFiles) {
                    Copy-PathExact $RollbackRoot $RuntimeRoot $name
                }
                if ($ExistingNames.Count -gt 0) {
                    $expectedRollback = Get-RelativeManifest $RollbackRoot $ExistingNames
                    $restoredManifest = Get-RelativeManifest $RuntimeRoot $ExistingNames
                    Assert-ManifestsEqual $expectedRollback $restoredManifest "Rollback restore"
                }
                Write-Host "[ROLLBACK] Previous runtime set restored and SHA-256 verified." -ForegroundColor Yellow
            } catch {
                Write-Host "[WARN] Automatic rollback restore failed. Verified backup remains at: $RollbackRoot" -ForegroundColor Yellow
                Write-Host "[WARN] Do not start this component until the runtime set is restored together." -ForegroundColor Yellow
            }
        } else {
            Write-Host "[WARN] Failed deployment paths could not be fully cleared. Verified backup remains at: $RollbackRoot" -ForegroundColor Yellow
            Write-Host "[WARN] Automatic restore was intentionally skipped to avoid a mixed old/new runtime set." -ForegroundColor Yellow
        }
    }

    throw $DeploymentError
}
