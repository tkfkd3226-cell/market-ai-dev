[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Entry = Join-Path $Root "start-local-server.pyw"
$Icon = Join-Path $Root "InvestmentLocalSuite.ico"
$SignScript = Join-Path $Root "Sign-InvestmentLocalSuite.ps1"
$BuildRequirementsLockPath = Join-Path $Root "requirements-build-lock.txt"
$DeployHelper = Join-Path $Root "tools\runtime-deploy.ps1"
$RuntimeStopHelper = Join-Path $Root "tools\runtime-stop.ps1"
$CleanDevHelper = Join-Path $Root "tools\clean-dev-artifacts.ps1"
$RuntimeRoot = Join-Path (Split-Path -Parent $Root) "market-ai"
$OutputExe = Join-Path $RuntimeRoot "InvestmentLocalSuite.exe"
$OutputInternal = Join-Path $RuntimeRoot "_suite_internal"

$PyInstallerVersion = "6.22.2"
$Revision = "phase4-r1-suite-clean-dev-staged-sign-auto-stop-sibling-deploy-pwsh51"

$BuildBase = Join-Path $env:TEMP "InvestmentLocalSuite\LauncherOnedir"
$BuildVenv = Join-Path $BuildBase "venv"
$DistPath = Join-Path $BuildBase "dist"
$WorkPath = Join-Path $BuildBase "work"
$SpecPath = Join-Path $BuildBase "spec"
$StagedRoot = Join-Path $DistPath "InvestmentLocalSuite"
$StagedExe = Join-Path $StagedRoot "InvestmentLocalSuite.exe"
$StagedInternal = Join-Path $StagedRoot "_suite_internal"

function Write-Step([string]$Message) {
    Write-Host "[SUITE-EDR-BUILD] $Message"
}

function Fail([string]$Message) {
    throw $Message
}

function Invoke-Python([string[]]$Arguments) {
    & $script:PythonExe @script:PythonPrefix @Arguments
    if ($LASTEXITCODE -ne 0) {
        Fail "Python command failed with exit code $LASTEXITCODE."
    }
}

function Invoke-PythonSource([string]$PythonExe, [string[]]$PythonPrefix, [string]$Code, [string[]]$CodeArguments = @(), [switch]$Quiet) {
    $tempScript = Join-Path ([System.IO.Path]::GetTempPath()) ("market-ai-build-" + [Guid]::NewGuid().ToString("N") + ".py")
    try {
        [System.IO.File]::WriteAllText(
            $tempScript,
            $Code,
            (New-Object System.Text.UTF8Encoding($false))
        )
        if ($Quiet) {
            & $PythonExe @PythonPrefix $tempScript @CodeArguments 1>$null 2>$null
        } else {
            & $PythonExe @PythonPrefix $tempScript @CodeArguments | Out-Host
        }
        return [int]$LASTEXITCODE
    } finally {
        Remove-Item -LiteralPath $tempScript -Force -ErrorAction SilentlyContinue
    }
}

function Require-Path([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Fail "$Label not found: $Path"
    }
}

Require-Path $Entry "consolidated launcher source"
Require-Path $Icon "approved icon"
Require-Path $SignScript "code-signing helper"
Require-Path $BuildRequirementsLockPath "Windows build-tool lock"
Require-Path $DeployHelper "runtime deployment helper"
Require-Path $RuntimeStopHelper "runtime stop helper"
Require-Path $CleanDevHelper "clean-dev artifact helper"
Require-Path $RuntimeRoot "sibling market-ai runtime"
if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals((Split-Path -Leaf $Root), "market-ai-dev")) {
    Fail "Investment Local Suite build/deploy must run from a repository directory named market-ai-dev. Resolved: $Root"
}
Write-Step "Sibling runtime preflight PASS: $RuntimeRoot"
Write-Step "Cleaning stale generated/runtime artifacts from market-ai-dev before build..."
& $CleanDevHelper
if ($LASTEXITCODE -ne 0) { Fail "Clean-dev pre-build cleanup failed." }

$buildLockEntries = @(Get-Content -LiteralPath $BuildRequirementsLockPath | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith("#") })
if (-not ($buildLockEntries -contains ("PyInstaller==" + $PyInstallerVersion))) {
    Fail "requirements-build-lock.txt PyInstaller version does not match build script: $PyInstallerVersion"
}

$sourceText = [System.IO.File]::ReadAllText($Entry, [System.Text.Encoding]::UTF8)
$forbidden = @(
    "_load_legacy_module",
    "LEGACY_BACKUP_NAME",
    'python_exe, "-m", "uvicorn"',
    'python_exe, "-m", "http.server"',
    '"pip", "install"'
)
foreach ($token in $forbidden) {
    if ($sourceText.Contains($token)) {
        Fail "Consolidated source still contains forbidden legacy runtime token: $token"
    }
}

if (-not $sourceText.Contains('MARKET_AI_PROCESS = "MarketAI.exe"')) {
    Fail "Consolidated source does not contain the MarketAI.exe runtime contract."
}
if (-not $sourceText.Contains("http.server.ThreadingHTTPServer")) {
    Fail "Consolidated source does not contain embedded Dashboard HTTP."
}
if (-not $sourceText.Contains("REMOTE_GET_PROXY_PORT = 8002")) {
    Fail "Consolidated source does not contain the remote GET-only proxy port contract."
}
if (-not $sourceText.Contains("RemoteGetMarketAiProxyHandler")) {
    Fail "Consolidated source does not contain the remote GET-only proxy boundary."
}
if (-not $sourceText.Contains("_disable_direct_market_ai_serve")) {
    Fail "Consolidated source does not contain the fail-closed legacy Serve cleanup boundary."
}
if (-not $sourceText.Contains('[cli, "serve", "off"]')) {
    Fail "Consolidated source does not disable the canonical Serve root when the GET-only boundary fails."
}
if (-not $sourceText.Contains('if self._serve_exposes_backend_directly(serve_output):')) {
    Fail "Consolidated source does not reject mixed Serve configurations that still expose :8001."
}
if (-not $sourceText.Contains('reason="direct :8001 mapping detected alongside remote configuration"')) {
    Fail "Consolidated source is missing the mixed :8001/:8002 fail-closed repair contract."
}
foreach ($remoteGuardToken in @(
    "remote_quote_max_tickers_per_client = 64",
    "def _prepare_remote_quote_query",
    "RemoteGetMarketAiProxyHandler",
    'return "remote-" + hashlib.sha256'
)) {
    if (-not $sourceText.Contains($remoteGuardToken)) {
        Fail "Consolidated source is missing remote quote boundary contract: $remoteGuardToken"
    }
}
if ($sourceText.Contains("remote_quote_leases") -or $sourceText.Contains("remote_quote_lock")) {
    Fail "Consolidated source must not mirror backend quote lease state in the :8002 proxy."
}

$bootstrapProbe = @'
import platform
import sys

machine = platform.machine().lower()
ok = sys.version_info[:2] == (3, 13) and machine in {"amd64", "x86_64"}
raise SystemExit(0 if ok else 1)
'@

$py = Get-Command py.exe -ErrorAction SilentlyContinue
$python = Get-Command python.exe -ErrorAction SilentlyContinue
$BootstrapPythonExe = $null
$BootstrapPythonPrefix = @()
if ($py) {
    $probeExit = Invoke-PythonSource -PythonExe $py.Source -PythonPrefix @("-3.13") -Code $bootstrapProbe -Quiet
    if ($probeExit -eq 0) {
        $BootstrapPythonExe = $py.Source
        $BootstrapPythonPrefix = @("-3.13")
    }
}
if (-not $BootstrapPythonExe -and $python) {
    $probeExit = Invoke-PythonSource -PythonExe $python.Source -PythonPrefix @() -Code $bootstrapProbe -Quiet
    if ($probeExit -eq 0) {
        $BootstrapPythonExe = $python.Source
        $BootstrapPythonPrefix = @()
    }
}
if (-not $BootstrapPythonExe) {
    Fail "Investment Local Suite build requires an available Python 3.13 x64 runtime (py -3.13 or python.exe)."
}

if (Test-Path -LiteralPath $BuildBase) {
    Remove-Item -LiteralPath $BuildBase -Recurse -Force
}
New-Item -ItemType Directory -Path $BuildBase -Force | Out-Null

Write-Step "Revision: $Revision"
Write-Step "Build mode: onedir + windowed + requireAdministrator"
Write-Step "Support directory: _suite_internal"
Write-Step "No onefile extraction at runtime."
Write-Step "No embedded/dynamic .phase1.bak execution."
Write-Step "Bootstrap Python: $BootstrapPythonExe $($BootstrapPythonPrefix -join ' ')"
Write-Step "Creating isolated Python 3.13 build venv..."
& $BootstrapPythonExe @BootstrapPythonPrefix -m venv $BuildVenv
if ($LASTEXITCODE -ne 0) {
    Fail "Failed to create isolated Investment Local Suite build venv."
}

$script:PythonExe = Join-Path $BuildVenv "Scripts\python.exe"
$script:PythonPrefix = @()
Require-Path $script:PythonExe "isolated build Python"
$probeExit = Invoke-PythonSource -PythonExe $script:PythonExe -PythonPrefix @() -Code $bootstrapProbe -Quiet
if ($probeExit -ne 0) {
    Fail "Isolated build venv is not Python 3.13 x64."
}
Write-Step "Python build baseline PASS: 3.13 x64"
Write-Step "Installing fully pinned PyInstaller/build-tool closure..."
Invoke-Python @("-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "-r", $BuildRequirementsLockPath)
Invoke-Python @("-m", "pip", "check")

$verifyBuildLockCode = @'
from importlib import metadata
from pathlib import Path
import re
import sys

errors = []
lock = Path(sys.argv[1])
for raw in lock.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    match = re.match(r"^([A-Za-z0-9_.-]+)==([^;\s]+)$", line)
    if not match:
        errors.append(f"unsupported build lock entry: {line}")
        continue
    name, expected = match.groups()
    try:
        actual = metadata.version(name)
    except metadata.PackageNotFoundError:
        errors.append(f"missing package: {name}=={expected}")
        continue
    if actual != expected:
        errors.append(f"version drift: {name} expected {expected}, got {actual}")
if errors:
    print("\n".join(errors))
    raise SystemExit(1)
print("Local Suite build lock exact-version verification PASS")
'@
$verifyExit = Invoke-PythonSource -PythonExe $script:PythonExe -PythonPrefix @() -Code $verifyBuildLockCode -CodeArguments @($BuildRequirementsLockPath)
if ($verifyExit -ne 0) {
    Fail "Local Suite build lock exact-version verification failed."
}

New-Item -ItemType Directory -Path $DistPath -Force | Out-Null
New-Item -ItemType Directory -Path $WorkPath -Force | Out-Null
New-Item -ItemType Directory -Path $SpecPath -Force | Out-Null

$Args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",
    "--uac-admin",
    "--name", "InvestmentLocalSuite",
    "--icon", $Icon,
    "--contents-directory", "_suite_internal",
    "--distpath", $DistPath,
    "--workpath", $WorkPath,
    "--specpath", $SpecPath,
    "--paths", $Root,
    "--add-data", "$Icon`:.",
    "--hidden-import", "tkinter",
    "--hidden-import", "tkinter.ttk",
    "--hidden-import", "tkinter.messagebox",
    $Entry
)

Write-Step "Building hardened InvestmentLocalSuite.exe..."
Invoke-Python $Args

Require-Path $StagedExe "staged InvestmentLocalSuite.exe"
Require-Path $StagedInternal "staged _suite_internal"

Write-Step "Applying Authenticode code signature to staged executable before runtime shutdown..."
& $SignScript -FilePath $StagedExe
$stagedSignature = Get-AuthenticodeSignature -FilePath $StagedExe
if ($stagedSignature.Status -ne "Valid") {
    Fail "Staged InvestmentLocalSuite.exe is not validly signed. Operating runtime was not modified."
}
Write-Step "Staged code signature VALID: $($stagedSignature.SignerCertificate.Subject)"

# Keep the operating suite available during the comparatively expensive build and
# signing steps. Stop it only after a complete signed staging set exists. The
# helper invokes the tray's exact "서버·Bridge 종료" command and preserves eFriend.
Write-Step "Stopping operating Local Suite / MarketAI / Bridge via shared tray shutdown contract..."
& $RuntimeStopHelper

Write-Step "Deploying signed staged Local Suite runtime to sibling market-ai..."
try {
    & $DeployHelper -Component "InvestmentLocalSuite" -StagingRoot $StagedRoot
} catch {
    Write-Host "[SUITE-EDR-BUILD] Runtime deployment failed. The common deployment helper owns rollback/recovery." -ForegroundColor Yellow
    Write-Host "[INFO] PyInstaller diagnostics retained at: $BuildBase" -ForegroundColor Yellow
    throw
}

Require-Path $OutputExe "deployed InvestmentLocalSuite.exe"
Require-Path $OutputInternal "deployed _suite_internal"
$deployedSignature = Get-AuthenticodeSignature -FilePath $OutputExe
if ($deployedSignature.Status -ne "Valid") {
    Fail "Deployed InvestmentLocalSuite.exe signature is not valid after deployment."
}

$exeInfo = Get-Item -LiteralPath $OutputExe
$runtimeFiles = @(Get-ChildItem -LiteralPath $OutputInternal -File -Recurse -ErrorAction SilentlyContinue)

Write-Step "Final clean-dev verification before success..."
& $CleanDevHelper
if ($LASTEXITCODE -ne 0) { Fail "Clean-dev post-build cleanup failed." }
& $CleanDevHelper -VerifyOnly
if ($LASTEXITCODE -ne 0) { Fail "Clean-dev post-build verification failed." }

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host " INVESTMENT LOCAL SUITE BUILD + DEPLOY : SUCCESS" -ForegroundColor Green
Write-Host "==============================================================" -ForegroundColor Green
Write-Host " Build        : PASS"
Write-Host " Code sign    : PASS (staged before operating runtime stop)"
Write-Host " Runtime stop : PASS (서버·Bridge 종료 contract / eFriend preserved)"
Write-Host " Clean replace: PASS (_suite_internal replaced as one directory)"
Write-Host " Deploy verify: PASS (SHA-256 manifest verified by runtime-deploy.ps1)"
Write-Host " Dev cleanup   : PASS (runtime/build artifacts absent from market-ai-dev)"
Write-Host " Target       : $RuntimeRoot"
Write-Host " Suite EXE    : UPDATED"
Write-Host " _suite_internal : REPLACED CLEAN"
Write-Host (" EXE size     : {0:N1} MB" -f ($exeInfo.Length / 1MB))
Write-Host " Runtime files: $($runtimeFiles.Count) files under _suite_internal"
Write-Host " Mode         : onedir / windowed / requireAdministrator"
Write-Host " Signature    : VALID"
Write-Host " Market AI / Bridge runtime : external sibling market-ai resources; not required in dev root"
Write-Host " DB / .env    : preserved / not deployed"
Write-Host "==============================================================" -ForegroundColor Green
Write-Host "운영 반영까지 완전히 완료되었습니다." -ForegroundColor Green
