from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "tools" / "runtime-deploy.ps1"
STOP = ROOT / "tools" / "runtime-stop.ps1"
CLEAN = ROOT / "tools" / "clean-dev-artifacts.ps1"
GITIGNORE = ROOT / ".gitignore"
BUILD_MARKET = ROOT / "build-market-ai.ps1"
BUILD_SUITE = ROOT / "build-investment-local-suite.ps1"
BUILD_BRIDGE = ROOT / "build-kis-bridge-release.bat"
BRIDGE_PROJECT = ROOT / "KisKospi200Bridge" / "KisKospi200Bridge.csproj"
SUITE_SOURCE = ROOT / "start-local-server.pyw"

ROOT_RUNTIME_FILES = (
    "MarketAI.exe",
    "InvestmentLocalSuite.exe",
    "KisKospi200Bridge.exe",
    "KisKospi200Bridge.exe.config",
    "AxInterop.ITGExpertCtlLib.dll",
    "Interop.ITGExpertCtlLib.dll",
)


def _source() -> str:
    return DEPLOY.read_text(encoding="utf-8-sig")


# Runtime deploy scope and integrity
def test_runtime_deploy_helper_targets_only_sibling_runtime_and_fixed_components():
    source = _source()
    for token in (
        '$ParentRoot = Split-Path -Parent $DevRoot',
        '$RuntimeRoot = Join-Path $ParentRoot "market-ai"',
        'README.md',
        'Test-PathInside $StagingRoot $RuntimeRoot',
        'ValidateSet("MarketAI", "InvestmentLocalSuite", "KisBridge")',
        '"MarketAI.exe"',
        '"_internal"',
        '"InvestmentLocalSuite.exe"',
        '"_suite_internal"',
        '"AxInterop.ITGExpertCtlLib.dll"',
        '"Interop.ITGExpertCtlLib.dll"',
        '"KisKospi200Bridge.exe.config"',
        '"KisKospi200Bridge.exe"',
    ):
        assert token in source
    assert "RuntimeRoot" not in source.split("param(", 1)[1].split(")", 1)[0]


def test_runtime_deploy_helper_enforces_clean_support_directory_replacement():
    source = _source()
    assert 'Remove-PathExact $RuntimeRoot $directoryName' in source
    assert 'foreach ($name in @($spec.CopyOrder))' in source
    assert 'CopyOrder = @("_internal", "MarketAI.exe")' in source
    assert 'CopyOrder = @("_suite_internal", "InvestmentLocalSuite.exe")' in source


def test_runtime_deploy_helper_verifies_deploy_and_temp_rollback_integrity():
    source = _source()
    assert '$RollbackBase = Join-Path $env:TEMP "InvestmentLocalSuite\\RuntimeDeployRollback"' in source
    assert 'Get-FileHash -Algorithm SHA256' in source
    assert 'Assert-ManifestsEqual $runtimeBefore $backupManifest "Rollback backup"' in source
    assert 'Assert-ManifestsEqual $stagedManifest $deployedManifest "Deployed runtime"' in source
    assert 'Assert-ManifestsEqual $expectedRollback $restoredManifest "Rollback restore"' in source
    assert '_runtime-backup' not in source


def test_runtime_deploy_helper_has_non_mutating_plan_gate():
    source = _source()
    plan_index = source.index('if ($PlanOnly)')
    backup_index = source.index('$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"')
    assert plan_index < backup_index


def test_build_dependency_locks_are_exact_and_shared_without_freezing_log_text():
    runtime_lock = (ROOT / "requirements-lock.txt").read_text(encoding="utf-8")
    build_lock = (ROOT / "requirements-build-lock.txt").read_text(encoding="utf-8")
    market = (ROOT / "build-market-ai.ps1").read_text(encoding="utf-8-sig")
    suite = (ROOT / "build-investment-local-suite.ps1").read_text(encoding="utf-8-sig")

    runtime_entries = [line.strip() for line in runtime_lock.splitlines() if line.strip() and not line.startswith("#")]
    build_entries = [line.strip() for line in build_lock.splitlines() if line.strip() and not line.startswith("#")]
    assert runtime_entries and build_entries
    assert all("==" in line for line in runtime_entries + build_entries)

    assert "requirements-lock.txt" in market
    assert "requirements-build-lock.txt" in market
    assert "requirements-build-lock.txt" in suite
    assert "$BuildVenv" in market
    assert 'Invoke-Python @("-m", "pip", "check")' in market
    assert 'Invoke-Python @("-m", "pip", "check")' in suite
    assert '$env:MARKET_AI_DB_PATH = $SmokeDbPath' in market


# Runtime stop and component deployment
def test_runtime_stop_reuses_exact_local_suite_tray_server_bridge_command():
    source = STOP.read_text(encoding="utf-8-sig")
    suite_source = SUITE_SOURCE.read_text(encoding="utf-8-sig")
    assert "$WmCommand = 0x0111" in source
    assert "WM_COMMAND = 0x0111" in suite_source
    assert "ID_EXIT_KEEP_EFRIEND = 1004" in suite_source
    assert "self._request_exit(stop_efriend=False)" in suite_source
    assert "$LocalSuiteExitKeepEfriendCommand = 1004" in source
    assert 'Send-WindowMessageToProcesses "InvestmentLocalSuite"' in source
    assert 'Stop-ExpectedProcess "MarketAI"' in source
    assert 'Stop-ExpectedProcess "KisKospi200Bridge"' in source
    assert 'Stop-ExpectedProcess "InvestmentLocalSuite"' in source
    assert 'Stop-ExpectedProcess "efexpertmain"' not in source
    assert 'Stop-ExpectedProcess "efriendexpert"' not in source
    assert 'Stop-ExpectedProcess "xexpertgate"' not in source


def test_runtime_stop_elevates_only_when_owned_runtime_is_active():
    source = STOP.read_text(encoding="utf-8-sig")
    assert 'Get-Process -Name "InvestmentLocalSuite", "MarketAI", "KisKospi200Bridge"' in source
    assert 'Start-Process -FilePath "powershell.exe" -Verb RunAs' in source
    assert '-and -not $ElevatedChild' in source


def test_runtime_stop_never_force_kills_unrelated_port_owner():
    source = STOP.read_text(encoding="utf-8-sig")
    assert "$RuntimePorts = @(8000, 8001, 8002)" in source
    assert "Assert-NoUnexpectedRuntimePortOwners" in source
    assert '$RuntimeRoot = Join-Path (Split-Path -Parent $DevRoot) "market-ai"' in source
    assert "Test-PathInside $processPath $RuntimeRoot" in source
    assert 'taskkill.exe /F /PID' in source
    assert 'taskkill.exe /F /IM' not in source
    assert source.index("Assert-NoUnexpectedRuntimePortOwners") < source.index('Stop-ExpectedProcess "MarketAI"')


def test_market_build_stops_runtime_only_after_staging_and_before_smoke_and_deploy():
    source = BUILD_MARKET.read_text(encoding="utf-8-sig")
    build_index = source.index('Invoke-Python $Args')
    staged_check_index = source.index('Test-Path $StagedExe')
    stop_index = source.index('& $RuntimeStopHelper')
    smoke_index = source.index('Start-Process `')
    deploy_index = source.index('& $DeployHelper -Component "MarketAI" -StagingRoot $StagedRoot')
    assert build_index < staged_check_index < stop_index < smoke_index < deploy_index


def test_local_suite_build_is_clean_dev_and_deploys_signed_staging_set_to_sibling_runtime():
    source = BUILD_SUITE.read_text(encoding="utf-8-sig")
    assert '$RuntimeRoot = Join-Path (Split-Path -Parent $Root) "market-ai"' in source
    assert '$OutputExe = Join-Path $RuntimeRoot "InvestmentLocalSuite.exe"' in source
    assert '$OutputInternal = Join-Path $RuntimeRoot "_suite_internal"' in source
    assert '$MarketAIExe = Join-Path $Root "MarketAI.exe"' not in source
    assert '$MarketAIInternal = Join-Path $Root "_internal"' not in source
    assert '$BridgeExe = Join-Path $Root "KisKospi200Bridge.exe"' not in source
    assert '_runtime-backup' not in source
    assert source.index('& $SignScript -FilePath $StagedExe') < source.index('& $RuntimeStopHelper')
    assert source.index('& $RuntimeStopHelper') < source.index('& $DeployHelper -Component "InvestmentLocalSuite" -StagingRoot $StagedRoot')


def test_bridge_build_uses_sibling_runtime_and_common_stop_deploy_helpers():
    batch = BUILD_BRIDGE.read_text(encoding="utf-8-sig")
    project = BRIDGE_PROJECT.read_text(encoding="utf-8-sig")
    assert 'DeployBridgeRuntimeToMarketAiRoot' not in project
    assert 'AfterTargets="Build"' not in project
    assert 'for %%I in ("%ROOT%\\..\\market-ai") do set "RUNTIME_ROOT=%%~fI"' in batch
    assert 'set "DEPLOY_HELPER=%ROOT%\\tools\\runtime-deploy.ps1"' in batch
    assert 'set "STOP_HELPER=%ROOT%\\tools\\runtime-stop.ps1"' in batch
    assert '-Component KisBridge -StagingRoot "%RELEASE_DIR%"' in batch


# Clean dev tree and build hygiene
def test_canonical_dev_tree_contains_no_operating_runtime_or_generated_build_artifacts():
    for name in ROOT_RUNTIME_FILES:
        assert not (ROOT / name).exists(), name
    for name in ("_internal", "_suite_internal", "_runtime-backup"):
        assert not (ROOT / name).exists(), name
    assert not (ROOT / "KisKospi200Bridge" / "bin").exists()
    assert not (ROOT / "KisKospi200Bridge" / "obj").exists()


def test_clean_dev_helper_removes_only_generated_artifacts_and_preserves_db_data():
    source = CLEAN.read_text(encoding="utf-8-sig")
    for name in ROOT_RUNTIME_FILES:
        assert f'"{name}"' in source
    assert '"_internal", "_suite_internal", "_runtime-backup", ".pytest_cache"' in source
    assert '"KisKospi200Bridge\\bin"' in source
    assert '"KisKospi200Bridge\\obj"' in source
    assert '"__pycache__"' in source
    assert '".pyc", ".pyo"' in source
    assert "market_signal.db" not in source
    assert "dashboard_quote_universe.json" not in source
    assert "$VerifyOnly" in source


def test_gitignore_blocks_generated_runtime_without_ignoring_frozen_pyd_extensions():
    source = GITIGNORE.read_text(encoding="utf-8")
    rules = {
        line.strip()
        for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    for token in (
        "/MarketAI.exe",
        "/InvestmentLocalSuite.exe",
        "/KisKospi200Bridge.exe",
        "/KisKospi200Bridge.exe.config",
        "/AxInterop.ITGExpertCtlLib.dll",
        "/Interop.ITGExpertCtlLib.dll",
        "/_internal/",
        "/_suite_internal/",
        "/_runtime-backup/",
        "KisKospi200Bridge/bin/",
        "KisKospi200Bridge/obj/",
        "__pycache__/",
        ".pytest_cache/",
        "*.pyc",
        "*.pyo",
    ):
        assert token in rules
    assert "*.py[cod]" not in rules
    assert "*.pyd" not in rules


def test_all_three_builds_clean_before_build_and_verify_clean_state_afterward():
    market = BUILD_MARKET.read_text(encoding="utf-8-sig")
    suite = BUILD_SUITE.read_text(encoding="utf-8-sig")
    bridge = BUILD_BRIDGE.read_text(encoding="utf-8-sig")

    for source, build_call in (
        (market, "Invoke-Python $Args"),
        (suite, "Invoke-Python $Args"),
    ):
        assert source.index("& $CleanDevHelper") < source.index(build_call)
        assert source.index(build_call) < source.rindex("& $CleanDevHelper -VerifyOnly")

    build_call = '"!MSBUILD_EXE!" "%SOLUTION%"'
    assert bridge.index('"%CLEAN_HELPER%"') < bridge.index(build_call)
    verify_calls = [
        token for token in ('-VerifyOnly', '/VerifyOnly') if token in bridge
    ]
    assert verify_calls
    assert bridge.index(build_call) < max(bridge.rindex(token) for token in verify_calls)


def test_developer_db_is_retained_but_not_a_build_prerequisite():
    market = BUILD_MARKET.read_text(encoding="utf-8-sig")
    assert "Existing market_signal.db not found. Build aborted" not in market
    assert "$env:MARKET_AI_DB_PATH = $SmokeDbPath" in market
