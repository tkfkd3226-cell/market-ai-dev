@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
set "ROOT=%CD%"
set "SOLUTION=%ROOT%\KisKospi200Bridge.sln"
set "PROJECT_DIR=%ROOT%\KisKospi200Bridge"
set "RELEASE_DIR=%PROJECT_DIR%\bin\x86\Release"
for %%I in ("%ROOT%\..\market-ai") do set "RUNTIME_ROOT=%%~fI"
set "TARGET_EXE=%RUNTIME_ROOT%\KisKospi200Bridge.exe"
set "DEPLOY_HELPER=%ROOT%\tools\runtime-deploy.ps1"
set "STOP_HELPER=%ROOT%\tools\runtime-stop.ps1"
set "CLEAN_HELPER=%ROOT%\tools\clean-dev-artifacts.ps1"
set "MSBUILD_EXE="
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "BUILD_REVISION=phase4-r1-bridge-clean-dev-sibling-auto-deploy"
for %%I in ("%ROOT%") do set "DEV_LEAF=%%~nxI"

if /i not "%DEV_LEAF%"=="market-ai-dev" (
    echo [ERROR] KIS Bridge build/deploy must run from a directory named market-ai-dev.
    echo [ERROR] Resolved: %ROOT%
    exit /b 1
)
if not exist "%RUNTIME_ROOT%\" (
    echo [ERROR] Sibling market-ai runtime directory not found: %RUNTIME_ROOT%
    exit /b 1
)
if not exist "%RUNTIME_ROOT%\README.md" (
    echo [ERROR] Sibling market-ai runtime marker README.md not found: %RUNTIME_ROOT%\README.md
    exit /b 1
)
if not exist "%DEPLOY_HELPER%" (
    echo [ERROR] Runtime deployment helper not found: %DEPLOY_HELPER%
    exit /b 1
)
if not exist "%STOP_HELPER%" (
    echo [ERROR] Runtime stop helper not found: %STOP_HELPER%
    exit /b 1
)
if not exist "%CLEAN_HELPER%" (
    echo [ERROR] Clean-dev helper not found: %CLEAN_HELPER%
    exit /b 1
)

echo [BRIDGE-BUILD] Revision: %BUILD_REVISION%
echo [BRIDGE-BUILD] Sibling runtime preflight PASS: %RUNTIME_ROOT%
echo [INFO]  Cleaning stale generated/runtime artifacts from market-ai-dev before build...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CLEAN_HELPER%"
if errorlevel 1 (
    echo [ERROR] Clean-dev pre-build cleanup failed.
    exit /b 1
)

if /i "%~1"=="--ensure" (
    if exist "%TARGET_EXE%" (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "$exe=Get-Item -LiteralPath '%TARGET_EXE%'; $src=Get-ChildItem -LiteralPath '%PROJECT_DIR%' -Recurse -File | Where-Object { $_.Extension -in '.cs','.csproj','.resx','.config','.manifest','.ico' }; if(($src | Measure-Object LastWriteTimeUtc -Maximum).Maximum -le $exe.LastWriteTimeUtc){exit 0}else{exit 1}" >nul 2>nul
        if not errorlevel 1 (
            if exist "%RUNTIME_ROOT%\AxInterop.ITGExpertCtlLib.dll" if exist "%RUNTIME_ROOT%\Interop.ITGExpertCtlLib.dll" if exist "%RUNTIME_ROOT%\KisKospi200Bridge.exe.config" (
                echo [OK]    KIS Bridge sibling runtime is up to date.
                exit /b 0
            )
        )
    )
)

echo [BUILD] KIS KOSPI200 Bridge Release x86
echo [INFO]  Solution: %SOLUTION%
echo [INFO]  Searching for MSBuild...

for /f "delims=" %%I in ('where msbuild 2^>nul') do if not defined MSBUILD_EXE set "MSBUILD_EXE=%%I"

if not defined MSBUILD_EXE (
    if exist "!VSWHERE!" (
        echo [INFO]  vswhere: !VSWHERE!
        for /f "usebackq delims=" %%I in (`"!VSWHERE!" -latest -products * -requires Microsoft.Component.MSBuild -find MSBuild\**\Bin\MSBuild.exe 2^>nul`) do if not defined MSBUILD_EXE set "MSBUILD_EXE=%%I"
    ) else (
        echo [INFO]  vswhere.exe not found at: !VSWHERE!
    )
)

if not defined MSBUILD_EXE (
    for %%E in (Community Professional Enterprise BuildTools) do (
        if not defined MSBUILD_EXE if exist "%ProgramFiles%\Microsoft Visual Studio\2022\%%E\MSBuild\Current\Bin\MSBuild.exe" set "MSBUILD_EXE=%ProgramFiles%\Microsoft Visual Studio\2022\%%E\MSBuild\Current\Bin\MSBuild.exe"
        if not defined MSBUILD_EXE if exist "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\%%E\MSBuild\Current\Bin\MSBuild.exe" set "MSBUILD_EXE=%ProgramFiles(x86)%\Microsoft Visual Studio\2022\%%E\MSBuild\Current\Bin\MSBuild.exe"
    )
)

if not defined MSBUILD_EXE (
    echo [ERROR] MSBuild was not found.
    echo [ERROR] Checked PATH, vswhere, and Visual Studio 2022 default install paths.
    echo [ACTION] Install Visual Studio 2022 or Build Tools 2022 with '.NET desktop development'.
    echo [ACTION] Then run build-kis-bridge-release.bat again and review the MSBuild output if it still fails.
    exit /b 1
)

echo [OK]    MSBuild: !MSBUILD_EXE!
echo [INFO]  Removing Windows download blocking from Bridge source files...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; Get-Item -LiteralPath '%SOLUTION%' | Unblock-File; Get-ChildItem -LiteralPath '%PROJECT_DIR%' -Recurse -File | Unblock-File; exit 0"
if errorlevel 1 (
    echo [WARN]  Could not fully remove Windows file blocking. Build will still be attempted.
) else (
    echo [OK]    Bridge source files are unblocked.
)
echo [INFO]  Building Release^|x86...
set "VSLANG=1033"

"!MSBUILD_EXE!" "%SOLUTION%" /t:Build /m /p:Configuration=Release /p:Platform=x86 /p:PreferredUILang=en-US /nologo /verbosity:quiet /clp:ErrorsOnly
if errorlevel 1 (
    echo [ERROR] KIS Bridge build failed.
    exit /b 1
)

echo [OK]    KIS Bridge build succeeded.

for %%F in (KisKospi200Bridge.exe KisKospi200Bridge.exe.config AxInterop.ITGExpertCtlLib.dll Interop.ITGExpertCtlLib.dll) do (
    if not exist "%RELEASE_DIR%\%%F" (
        echo [ERROR] Build completed but staged Bridge runtime file is missing: %RELEASE_DIR%\%%F
        exit /b 1
    )
)

echo [INFO]  Stopping operating Local Suite / MarketAI / Bridge via tray shutdown contract...
powershell -NoProfile -ExecutionPolicy Bypass -File "%STOP_HELPER%"
if errorlevel 1 (
    echo [ERROR] Runtime stop helper failed. Operating runtime was not deployed.
    exit /b 1
)

echo [INFO]  Deploying verified Bridge runtime set to sibling market-ai...
powershell -NoProfile -ExecutionPolicy Bypass -File "%DEPLOY_HELPER%" -Component KisBridge -StagingRoot "%RELEASE_DIR%"
if errorlevel 1 (
    echo [ERROR] KIS Bridge runtime deployment failed. Review rollback diagnostics above.
    exit /b 1
)

for %%F in (KisKospi200Bridge.exe KisKospi200Bridge.exe.config AxInterop.ITGExpertCtlLib.dll Interop.ITGExpertCtlLib.dll) do (
    if not exist "%RUNTIME_ROOT%\%%F" (
        echo [ERROR] Deployment helper returned but operating runtime file is missing: %RUNTIME_ROOT%\%%F
        exit /b 1
    )
)
echo [INFO]  Removing local Bridge bin/obj and verifying clean dev tree...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CLEAN_HELPER%"
if errorlevel 1 (
    echo [ERROR] Clean-dev post-build cleanup failed. Operating runtime is already deployed and verified.
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%CLEAN_HELPER%" -VerifyOnly
if errorlevel 1 (
    echo [ERROR] Clean-dev post-build verification failed.
    exit /b 1
)

echo.
echo ==============================================================
echo  KIS BRIDGE BUILD + DEPLOY : SUCCESS
echo ==============================================================
echo  Build        : PASS ^(Release^|x86^)
echo  Runtime stop : PASS ^(server/Bridge shutdown contract; eFriend preserved^)
echo  Deploy verify: PASS ^(SHA-256 manifest verified by runtime-deploy.ps1^)
echo  Dev cleanup   : PASS ^(Bridge bin/obj and dev runtime artifacts removed^)
echo  Target       : %RUNTIME_ROOT%
echo  Bridge EXE   : UPDATED
echo  Interop set  : UPDATED ^(2 DLL + config^)
echo ==============================================================
echo  Operating runtime deployment is complete.
exit /b 0
