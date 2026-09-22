[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Entry = Join-Path $Root "run_market_ai.py"
$Icon = Join-Path $Root "InvestmentLocalSuite.ico"
$RequirementsPath = Join-Path $Root "requirements.txt"
$RequirementsLockPath = Join-Path $Root "requirements-lock.txt"
$BuildRequirementsLockPath = Join-Path $Root "requirements-build-lock.txt"
$MonitorPath = Join-Path $Root "monitor"
$DeployHelper = Join-Path $Root "tools\runtime-deploy.ps1"
$RuntimeStopHelper = Join-Path $Root "tools\runtime-stop.ps1"
$CleanDevHelper = Join-Path $Root "tools\clean-dev-artifacts.ps1"
$RuntimeRoot = Join-Path (Split-Path -Parent $Root) "market-ai"
$RuntimeExe = Join-Path $RuntimeRoot "MarketAI.exe"
$RuntimeInternal = Join-Path $RuntimeRoot "_internal"

$PyInstallerVersion = "6.22.2"
$BuildScriptRevision = "phase4-r1-clean-dev-auto-stop-staged-smoke-sibling-deploy-pwsh51"
$BuildBase = Join-Path $env:TEMP "InvestmentLocalSuite\MarketAI"
$BuildVenv = Join-Path $BuildBase "venv"
$DistPath = Join-Path $BuildBase "dist"
$WorkPath = Join-Path $BuildBase "work"
$SpecPath = Join-Path $BuildBase "spec"
$StagedRoot = Join-Path $DistPath "MarketAI"
$StagedExe = Join-Path $StagedRoot "MarketAI.exe"
$StagedInternal = Join-Path $StagedRoot "_internal"

function Write-Step([string]$Message) {
    Write-Host "[MARKETAI-BUILD] $Message"
}

function Fail([string]$Message) {
    throw $Message
}

function Test-PortListening([int]$Port) {
    $target = ":$Port"
    $lines = @(& netstat.exe -ano -p tcp 2>$null)
    foreach ($line in $lines) {
        # Avoid collection .Count/index assumptions under StrictMode.
        # Match netstat's TCP LISTENING row directly and inspect only the local endpoint.
        if ($line -match '^\s*TCP\s+(\S+)\s+\S+\s+LISTENING\s+\d+\s*$') {
            $localEndpoint = $Matches[1]
            if ($localEndpoint.EndsWith($target)) {
                return $true
            }
        }
    }
    return $false
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

function Test-Importable([string]$ModuleName) {
    $code = @'
import importlib.util
import sys
raise SystemExit(0 if importlib.util.find_spec(sys.argv[1]) else 1)
'@
    $probeExit = Invoke-PythonSource -PythonExe $script:PythonExe -PythonPrefix $script:PythonPrefix -Code $code -CodeArguments @($ModuleName) -Quiet
    return $probeExit -eq 0
}

function Wait-Http([string]$Url, [int]$Seconds = 30, [int[]]$AcceptedStatus = @(200)) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($AcceptedStatus -contains [int]$response.StatusCode) {
                return $true
            }
        } catch {
            $statusCode = $null
            try {
                if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
                    $statusCode = [int]$_.Exception.Response.StatusCode
                }
            } catch {}
            if ($statusCode -ne $null -and $AcceptedStatus -contains $statusCode) {
                return $true
            }
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-SmokeProcess($Process) {
    if ($null -eq $Process) { return }
    try {
        if (-not $Process.HasExited) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
            try { $Process.WaitForExit(5000) | Out-Null } catch {}
        }
    } catch {}
}

if (-not (Test-Path -LiteralPath $DeployHelper -PathType Leaf)) {
    Fail "Runtime deployment helper not found: $DeployHelper"
}
if (-not (Test-Path -LiteralPath $RuntimeStopHelper -PathType Leaf)) {
    Fail "Runtime stop helper not found: $RuntimeStopHelper"
}
if (-not (Test-Path -LiteralPath $CleanDevHelper -PathType Leaf)) {
    Fail "Clean-dev artifact helper not found: $CleanDevHelper"
}
if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals((Split-Path -Leaf $Root), "market-ai-dev")) {
    Fail "Market AI build/deploy must run from a repository directory named market-ai-dev. Resolved: $Root"
}
if (-not (Test-Path -LiteralPath $RuntimeRoot -PathType Container)) {
    Fail "Sibling market-ai runtime directory not found: $RuntimeRoot"
}
Write-Step "Revision: $BuildScriptRevision"
Write-Step "Sibling runtime preflight PASS: $RuntimeRoot"
Write-Step "Cleaning stale generated/runtime artifacts from market-ai-dev before build..."
& $CleanDevHelper
if ($LASTEXITCODE -ne 0) { Fail "Clean-dev pre-build cleanup failed." }

if (-not (Test-Path $Entry)) {
    Fail "run_market_ai.py not found: $Entry"
}
if (-not (Test-Path $Icon)) {
    Fail "InvestmentLocalSuite.ico not found: $Icon"
}
if (-not (Test-Path $RequirementsPath)) {
    Fail "requirements.txt not found: $RequirementsPath"
}
if (-not (Test-Path $RequirementsLockPath)) {
    Fail "requirements-lock.txt not found: $RequirementsLockPath"
}
if (-not (Test-Path $BuildRequirementsLockPath)) {
    Fail "requirements-build-lock.txt not found: $BuildRequirementsLockPath"
}
$QuoteServicePath = Join-Path $Root "bridges\krx_quotes.py"
$AppSourcePath = Join-Path $Root "app.py"
$DashboardHoldingsPath = Join-Path $Root "bridges\dashboard_holdings.py"
$RunMarketAiSourcePath = Join-Path $Root "run_market_ai.py"
foreach ($requiredSource in @($QuoteServicePath, $AppSourcePath, $DashboardHoldingsPath, $RunMarketAiSourcePath)) {
    if (-not (Test-Path -LiteralPath $requiredSource)) {
        Fail "Required Market AI source not found: $requiredSource"
    }
}
$quoteSourceText = Get-Content -LiteralPath $QuoteServicePath -Raw
$appSourceText = Get-Content -LiteralPath $AppSourcePath -Raw
$holdingsSourceText = Get-Content -LiteralPath $DashboardHoldingsPath -Raw
$runMarketAiSourceText = Get-Content -LiteralPath $RunMarketAiSourcePath -Raw
foreach ($quoteGuardToken in @(
    'DEFAULT_MAX_REMOTE_CLIENTS = 16',
    'def is_remote_client_id',
    'def _evict_remote_leases_for_local_capacity_locked',
    'def local_dashboard_tickers',
    'candidate_bootstrap_active = self._bootstrap_active if is_remote else False',
    'bootstrap_active=candidate_bootstrap_active',
    'self._bootstrap_active = candidate_bootstrap_active'
)) {
    if (-not $quoteSourceText.Contains($quoteGuardToken)) {
        Fail "KrxQuoteService source is missing local-priority remote admission contract: $quoteGuardToken"
    }
}
foreach ($appGuardToken in @(
    'if not krx_quote_service.is_remote_client_id(client_id):',
    'with dashboard_universe_persist_lock:',
    'krx_quote_service.local_dashboard_tickers()'
)) {
    if (-not $appSourceText.Contains($appGuardToken)) {
        Fail "app.py is missing local-only durable Dashboard universe contract: $appGuardToken"
    }
}
if (-not $holdingsSourceText.Contains('_STATE_WRITE_LOCK = Lock()') -or -not $holdingsSourceText.Contains('with _STATE_WRITE_LOCK:')) {
    Fail "dashboard_holdings.py is missing serialized restart-state persistence."
}
if (-not $runMarketAiSourceText.Contains('host=DEFAULT_HOST') -or -not $runMarketAiSourceText.Contains('port=DEFAULT_PORT')) {
    Fail "run_market_ai.py must pin the full FastAPI backend to 127.0.0.1:8001."
}
if ($runMarketAiSourceText.Contains('os.environ.get("MARKET_AI_HOST"') -or $runMarketAiSourceText.Contains('os.environ.get("MARKET_AI_PORT"')) {
    Fail "run_market_ai.py must not allow environment variables to rebind the full FastAPI backend."
}
$buildLockEntries = @(Get-Content -LiteralPath $BuildRequirementsLockPath | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith("#") })
if (-not ($buildLockEntries -contains ("PyInstaller==" + $PyInstallerVersion))) {
    Fail "requirements-build-lock.txt PyInstaller version does not match build script: $PyInstallerVersion"
}
foreach ($monitorFile in @("index.html", "monitor.css", "monitor.js")) {
    $monitorFilePath = Join-Path $MonitorPath $monitorFile
    if (-not (Test-Path $monitorFilePath)) {
        Fail "Web Monitor asset not found: $monitorFilePath"
    }
}
$py = Get-Command py.exe -ErrorAction SilentlyContinue
$python = Get-Command python.exe -ErrorAction SilentlyContinue

$bootstrapProbe = @'
import platform
import sys

machine = platform.machine().lower()
ok = sys.version_info[:2] == (3, 13) and machine in {"amd64", "x86_64"}
raise SystemExit(0 if ok else 1)
'@

# Prefer the Windows launcher when it can actually resolve Python 3.13, but do
# not fail merely because py.exe exists without a registered 3.13 runtime. A
# directly installed python.exe is accepted only after the same exact baseline
# check, so the build cannot silently drift to 3.14+.
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
    Fail "Market AI build requires an available Python 3.13 x64 runtime (py -3.13 or python.exe)."
}

$probeExit = Invoke-PythonSource -PythonExe $BootstrapPythonExe -PythonPrefix $BootstrapPythonPrefix -Code $bootstrapProbe -Quiet
if ($probeExit -ne 0) {
    Fail "Market AI build requires Python 3.13 x64."
}
Write-Step "Python build baseline PASS: 3.13 x64"

if (Test-Path $BuildBase) {
    Remove-Item -LiteralPath $BuildBase -Recurse -Force
}
New-Item -ItemType Directory -Path $BuildBase -Force | Out-Null

Write-Step "Root: $Root"
Write-Step "Bootstrap Python: $BootstrapPythonExe $($BootstrapPythonPrefix -join ' ')"
Write-Step "PyInstaller: $PyInstallerVersion"
Write-Step "Temporary build root: $BuildBase"
Write-Step "Creating isolated temporary build venv..."
& $BootstrapPythonExe @BootstrapPythonPrefix -m venv $BuildVenv
if ($LASTEXITCODE -ne 0) {
    Fail "Failed to create isolated Market AI build venv."
}

$script:PythonExe = Join-Path $BuildVenv "Scripts\python.exe"
$script:PythonPrefix = @()
if (-not (Test-Path $script:PythonExe)) {
    Fail "Build venv Python was not created: $script:PythonExe"
}

Invoke-Python @("--version")
Write-Step "Installing fully pinned PyInstaller/build backend closure first..."
Invoke-Python @("-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "-r", $BuildRequirementsLockPath)
Write-Step "Checking pinned build-tool dependency compatibility..."
Invoke-Python @("-m", "pip", "check")
Write-Step "Installing fully pinned Market AI runtime closure without dependency resolution/build isolation..."
Invoke-Python @("-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "--no-build-isolation", "-r", $RequirementsLockPath)
Write-Step "Checking locked runtime dependency compatibility..."
Invoke-Python @("-m", "pip", "check")
Write-Step "Compatibility policy remains documented in requirements.txt; executable build inputs are the two full exact locks."

$verifyLockCode = @'
from importlib import metadata
from pathlib import Path
import re
import sys

errors = []
for lock_arg in sys.argv[1:]:
    lock = Path(lock_arg)
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^;\s]+)$", line)
        if not match:
            errors.append(f"unsupported lock entry: {line}")
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
print("runtime/build lock full-closure verification PASS")
'@
$verifyExit = Invoke-PythonSource -PythonExe $script:PythonExe -PythonPrefix @() -Code $verifyLockCode -CodeArguments @($RequirementsLockPath, $BuildRequirementsLockPath)
if ($verifyExit -ne 0) {
    Fail "Market AI runtime/build lock exact-version verification failed."
}

New-Item -ItemType Directory -Path $DistPath -Force | Out-Null
New-Item -ItemType Directory -Path $WorkPath -Force | Out-Null
New-Item -ItemType Directory -Path $SpecPath -Force | Out-Null

$Args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--console",
    "--name", "MarketAI",
    "--icon", $Icon,
    "--contents-directory", "_internal",
    "--distpath", $DistPath,
    "--workpath", $WorkPath,
    "--specpath", $SpecPath,
    "--paths", $Root,
    "--collect-submodules", "sqlalchemy.dialects",
    "--hidden-import", "uvicorn.logging",
    "--hidden-import", "uvicorn.loops.auto",
    "--hidden-import", "uvicorn.protocols.http.auto",
    "--hidden-import", "uvicorn.protocols.websockets.auto",
    "--hidden-import", "uvicorn.lifespan.on",
    "--add-data", "$MonitorPath;monitor"
)

# Packages with runtime discovery, package data, or native DLLs get explicit
# collection. Missing optional packages are skipped instead of breaking builds.
$CollectAllCandidates = @(
    "uvicorn",
    "yfinance",
    "exchange_calendars",
    "korean_lunar_calendar",
    "curl_cffi",
    "websockets",
    "certifi",
    "charset_normalizer",
    "tzdata"
)

foreach ($package in $CollectAllCandidates) {
    if (Test-Importable $package) {
        Write-Step "Collect all: $package"
        $Args += @("--collect-all", $package)
    } else {
        Write-Step "Optional package not installed; skip collect-all: $package"
    }
}

$Args += $Entry
Invoke-Python $Args

if (-not (Test-Path $StagedExe)) {
    Fail "PyInstaller completed but staged MarketAI.exe was not found: $StagedExe"
}
if (-not (Test-Path $StagedInternal)) {
    Fail "PyInstaller completed but staged _internal was not found: $StagedInternal"
}

Write-Step "Staged build created successfully."


# Build is intentionally completed before stopping the operating runtime. The
# shared stop helper then invokes the Local Suite tray's exact "서버·Bridge 종료"
# WM_COMMAND path when the suite is running, preserving eFriend Expert. Only after
# that lifecycle is verified do we occupy :8001 with the staged smoke runtime.
Write-Step "Preparing ports for staged smoke via shared Local Suite runtime-stop contract..."
& $RuntimeStopHelper
if (Test-PortListening 8001) {
    Fail "Port 8001 is still occupied after the runtime-stop contract. Operating runtime was not deployed."
}

$SmokeRuntimeRoot = Join-Path $BuildBase "smoke-runtime"
$SmokeDbDir = Join-Path $SmokeRuntimeRoot "db"
$SmokeDbPath = Join-Path $SmokeDbDir "market_signal.db"
New-Item -ItemType Directory -Path $SmokeDbDir -Force | Out-Null

$smokeOut = Join-Path $BuildBase "MarketAI-smoke.stdout.log"
$smokeErr = Join-Path $BuildBase "MarketAI-smoke.stderr.log"
$smoke = $null

$SmokeEnvNames = @(
    "MARKET_AI_HOME",
    "MARKET_AI_DB_PATH",
    "MARKET_AI_COLLECTOR_ENABLED",
    "MARKET_AI_NEWS_ENABLED",
    "MARKET_AI_AI_ENABLED",
    "MARKET_AI_SIGNAL_ENABLED"
)
$SmokeEnvBackup = @{}
foreach ($name in $SmokeEnvNames) {
    $SmokeEnvBackup[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

try {
    $env:MARKET_AI_HOME = $SmokeRuntimeRoot
    $env:MARKET_AI_DB_PATH = $SmokeDbPath
    $env:MARKET_AI_COLLECTOR_ENABLED = "false"
    $env:MARKET_AI_NEWS_ENABLED = "false"
    $env:MARKET_AI_AI_ENABLED = "false"
    $env:MARKET_AI_SIGNAL_ENABLED = "false"

    Write-Step "Starting staged MarketAI.exe isolated smoke test..."
    $smoke = Start-Process `
        -FilePath $StagedExe `
        -WorkingDirectory $StagedRoot `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $smokeOut `
        -RedirectStandardError $smokeErr

    if (-not (Wait-Http "http://127.0.0.1:8001/openapi.json" 35 @(200))) {
        $detail = ""
        if (Test-Path $smokeErr) {
            $detail = (Get-Content -LiteralPath $smokeErr -Tail 30 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
        }
        Fail "Staged MarketAI.exe did not serve /openapi.json within 35 seconds.`n$detail"
    }

    if (-not (Wait-Http "http://127.0.0.1:8001/monitor/" 10 @(200))) {
        Fail "Staged MarketAI.exe started, but /monitor/ did not return 200."
    }
    if (-not (Wait-Http "http://127.0.0.1:8001/monitor/monitor.css" 10 @(200))) {
        Fail "Staged MarketAI.exe started, but /monitor/monitor.css did not return 200."
    }
    if (-not (Wait-Http "http://127.0.0.1:8001/monitor/monitor.js" 10 @(200))) {
        Fail "Staged MarketAI.exe started, but /monitor/monitor.js did not return 200."
    }
    if (-not (Wait-Http "http://127.0.0.1:8001/api/market-data/snapshot" 10 @(200))) {
        Fail "Staged MarketAI.exe started, but /api/market-data/snapshot did not return 200."
    }
    if (-not (Wait-Http "http://127.0.0.1:8001/api/bridge/kis-efriend/quote-universe" 10 @(200))) {
        Fail "Staged MarketAI.exe started, but KRX quote-universe API did not return 200."
    }
    if (-not (Wait-Http "http://127.0.0.1:8001/api/market-data/krx-quotes?tickers=005930,000660&client_id=remote-build-smoke" 10 @(200))) {
        Fail "Staged MarketAI.exe started, but KRX quote snapshot API did not return 200."
    }
    if (-not (Wait-Http "http://127.0.0.1:8001/api/signal/latest?include_details=true" 10 @(200,404))) {
        Fail "Staged MarketAI.exe started, but /api/signal/latest did not return 200/404."
    }

    Write-Step "Staged executable smoke test PASS. Operating market-ai is still untouched."
} catch {
    Write-Host "[MARKETAI-BUILD] Staged smoke test failed. Operating market-ai was not modified." -ForegroundColor Yellow
    Write-Host "[INFO] PyInstaller diagnostics retained at: $BuildBase" -ForegroundColor Yellow
    throw
} finally {
    Stop-SmokeProcess $smoke
    Start-Sleep -Milliseconds 500
    foreach ($name in $SmokeEnvNames) {
        $previous = $SmokeEnvBackup[$name]
        if ($null -eq $previous) {
            [Environment]::SetEnvironmentVariable($name, $null, "Process")
        } else {
            [Environment]::SetEnvironmentVariable($name, [string]$previous, "Process")
        }
    }
}

if (Test-PortListening 8001) {
    Fail "Port 8001 is still occupied after staged smoke shutdown. Operating market-ai was not modified."
}

Write-Step "Deploying verified staged runtime to sibling market-ai via common deployment helper..."
try {
    & $DeployHelper -Component "MarketAI" -StagingRoot $StagedRoot
} catch {
    Write-Host "[MARKETAI-BUILD] Runtime deployment failed. The common deployment helper owns rollback/recovery." -ForegroundColor Yellow
    Write-Host "[INFO] PyInstaller diagnostics retained at: $BuildBase" -ForegroundColor Yellow
    throw
}

if (-not (Test-Path -LiteralPath $RuntimeExe -PathType Leaf)) {
    Fail "Deployment helper returned but operating MarketAI.exe is missing: $RuntimeExe"
}
if (-not (Test-Path -LiteralPath $RuntimeInternal -PathType Container)) {
    Fail "Deployment helper returned but operating _internal is missing: $RuntimeInternal"
}

$exeInfo = Get-Item -LiteralPath $RuntimeExe
$internalFiles = @((Get-ChildItem -LiteralPath $RuntimeInternal -File -Recurse -ErrorAction SilentlyContinue))

Write-Step "Final clean-dev verification before success..."
& $CleanDevHelper
if ($LASTEXITCODE -ne 0) { Fail "Clean-dev post-build cleanup failed." }
& $CleanDevHelper -VerifyOnly
if ($LASTEXITCODE -ne 0) { Fail "Clean-dev post-build verification failed." }

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host " MARKET AI BUILD + DEPLOY : SUCCESS" -ForegroundColor Green
Write-Host "==============================================================" -ForegroundColor Green
Write-Host " Build        : PASS"
Write-Host " Runtime stop : PASS (Local Suite 서버·Bridge 종료 contract / eFriend preserved)"
Write-Host " Smoke QA     : PASS (staged runtime)"
Write-Host " Clean replace: PASS (_internal replaced as one directory)"
Write-Host " Deploy verify: PASS (SHA-256 manifest verified by runtime-deploy.ps1)"
Write-Host " Dev cleanup   : PASS (runtime/build artifacts absent from market-ai-dev)"
Write-Host " Target       : $RuntimeRoot"
Write-Host " MarketAI.exe : UPDATED"
Write-Host " _internal    : REPLACED CLEAN"
Write-Host (" EXE size     : {0:N1} MB" -f ($exeInfo.Length / 1MB))
Write-Host " Runtime files: $($internalFiles.Count) files under _internal"
Write-Host " Deps         : runtime + build-tool full pinned closures verified in isolated Python 3.13 x64 venv"
Write-Host " DB           : isolated smoke DB used; operating market-ai\db\market_signal.db was not deployed or mutated"
Write-Host " .env         : sibling market-ai\.env preserved / not bundled"
Write-Host "==============================================================" -ForegroundColor Green
Write-Host "운영 반영까지 완전히 완료되었습니다." -ForegroundColor Green
