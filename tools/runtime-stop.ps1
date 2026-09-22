[CmdletBinding()]
param(
    [int]$GraceSeconds = 20,
    [switch]$ElevatedChild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$StopRevision = "phase3-runtime-stop-r1-tray-contract"
$ExpectedProcessNames = @(
    "InvestmentLocalSuite",
    "MarketAI",
    "KisKospi200Bridge"
)
$RuntimePorts = @(8000, 8001, 8002)
$WmCommand = 0x0111
$LocalSuiteExitKeepEfriendCommand = 1004
$BridgeExitMessage = 0x8200
$DevRoot = Split-Path -Parent $PSScriptRoot
$RuntimeRoot = Join-Path (Split-Path -Parent $DevRoot) "market-ai"

function Write-StopStep([string]$Message) {
    Write-Host "[RUNTIME-STOP] $Message"
}

function Fail([string]$Message) {
    throw $Message
}

function Get-NormalizedFullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd([char[]]@('\', '/'))
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

function Get-OwnedProcesses([string]$Name) {
    $owned = @()
    foreach ($process in @(Get-Process -Name $Name -ErrorAction SilentlyContinue)) {
        $processPath = $null
        try { $processPath = $process.Path } catch {}
        if ($processPath -and (Test-PathInside $processPath $RuntimeRoot)) {
            $owned += $process
        }
    }
    return $owned
}

function Test-IsAdministrator {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals((Split-Path -Leaf $DevRoot), "market-ai-dev")) {
    Fail "Runtime stop helper must run from a repository directory named market-ai-dev. Resolved: $DevRoot"
}
if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals((Split-Path -Leaf $RuntimeRoot), "market-ai")) {
    Fail "Runtime target must be the sibling directory named market-ai. Resolved: $RuntimeRoot"
}
if (-not (Test-Path -LiteralPath $RuntimeRoot -PathType Container)) {
    Fail "Sibling market-ai runtime directory not found: $RuntimeRoot"
}

# InvestmentLocalSuite.exe is built with requireAdministrator. A normal developer
# shell cannot reliably post WM_COMMAND to its elevated tray window because of
# Windows UIPI, nor can it terminate that process as a fallback. Elevate only when
# one of the owned runtime processes is actually active, then return to the parent
# build script after the elevated stop helper completes.
$activeOwnedRuntime = @(
    Get-Process -Name "InvestmentLocalSuite", "MarketAI", "KisKospi200Bridge" -ErrorAction SilentlyContinue
)
if ($activeOwnedRuntime.Count -gt 0 -and -not (Test-IsAdministrator) -and -not $ElevatedChild) {
    Write-StopStep "Owned runtime is active; requesting elevation for the shutdown lifecycle..."
    $elevatedArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"' + $PSCommandPath + '"'),
        "-GraceSeconds", [string]$GraceSeconds,
        "-ElevatedChild"
    )
    try {
        $elevated = Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $elevatedArgs -Wait -PassThru -ErrorAction Stop
    } catch {
        Fail "Runtime shutdown requires elevation while Investment Local Suite is active: $($_.Exception.Message)"
    }
    if ($elevated.ExitCode -ne 0) {
        Fail "Elevated runtime stop helper failed with exit code $($elevated.ExitCode)."
    }
    Write-StopStep "Elevated runtime shutdown completed successfully."
    return
}
if ($ElevatedChild -and -not (Test-IsAdministrator)) {
    Fail "Elevated runtime stop child did not receive an administrator token."
}

if (-not ("LocalSuiteBuildInterop" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class LocalSuiteBuildInterop
{
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

    public static int PostToProcess(int processId, uint message, int wParam)
    {
        int posted = 0;
        EnumWindows(delegate (IntPtr hWnd, IntPtr lParam)
        {
            uint ownerPid;
            GetWindowThreadProcessId(hWnd, out ownerPid);
            if (ownerPid == (uint)processId && PostMessage(hWnd, message, new IntPtr(wParam), IntPtr.Zero))
            {
                posted++;
            }
            return true;
        }, IntPtr.Zero);
        return posted;
    }
}
'@
}

function Test-ExpectedProcessesStopped {
    foreach ($name in $ExpectedProcessNames) {
        if (@(Get-OwnedProcesses $name).Count -gt 0) {
            return $false
        }
    }
    return $true
}

function Get-PortListenerPids([int]$Port) {
    $pids = @()
    $target = ":$Port"
    foreach ($line in @(& netstat.exe -ano -p tcp 2>$null)) {
        if ($line -match '^\s*TCP\s+(\S+)\s+\S+\s+LISTENING\s+(\d+)\s*$') {
            if ($Matches[1].EndsWith($target)) {
                $pids += [int]$Matches[2]
            }
        }
    }
    return @($pids | Sort-Object -Unique)
}

function Test-RuntimePortsFree {
    foreach ($port in $RuntimePorts) {
        if (@(Get-PortListenerPids $port).Count -gt 0) {
            return $false
        }
    }
    return $true
}

function Test-RuntimeStopped {
    return (Test-ExpectedProcessesStopped) -and (Test-RuntimePortsFree)
}

function Wait-RuntimeStopped([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        if (Test-RuntimeStopped) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    return (Test-RuntimeStopped)
}

function Send-WindowMessageToProcesses([string]$ProcessName, [uint32]$Message, [int]$WParam) {
    $posted = 0
    foreach ($process in (Get-OwnedProcesses $ProcessName)) {
        try {
            $posted += [LocalSuiteBuildInterop]::PostToProcess([int]$process.Id, $Message, $WParam)
        } catch {
            Write-Host "[WARN] Could not post Windows message to $ProcessName PID $($process.Id): $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    return $posted
}

function Stop-ExpectedProcess([string]$ProcessName) {
    $processes = @(Get-OwnedProcesses $ProcessName)
    if ($processes.Count -eq 0) {
        return
    }

    Write-StopStep "Fallback stop: $ProcessName"
    foreach ($process in $processes) {
        try {
            & taskkill.exe /PID ([string]$process.Id) /T 1>$null 2>$null
        } catch {}
    }

    $deadline = (Get-Date).AddSeconds(4)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-OwnedProcesses $ProcessName).Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 250
    }

    foreach ($process in @(Get-OwnedProcesses $ProcessName)) {
        try {
            & taskkill.exe /F /PID ([string]$process.Id) /T 1>$null 2>$null
        } catch {}
    }

    Start-Sleep -Milliseconds 300
    if (@(Get-OwnedProcesses $ProcessName).Count -gt 0) {
        Fail "Expected runtime process is still running after fallback stop: $ProcessName.exe"
    }
}

function Assert-NoUnexpectedRuntimePortOwners {
    foreach ($port in $RuntimePorts) {
        foreach ($listenerPid in (Get-PortListenerPids $port)) {
            $process = Get-Process -Id $listenerPid -ErrorAction SilentlyContinue
            if ($null -eq $process) {
                continue
            }
            $processPath = $null
            try { $processPath = $process.Path } catch {}
            $ownedName = $ExpectedProcessNames -contains $process.ProcessName
            $ownedPath = $processPath -and (Test-PathInside $processPath $RuntimeRoot)
            if (-not $ownedName -or -not $ownedPath) {
                Fail "Port $port is owned by unexpected process $($process.ProcessName).exe (PID $listenerPid). Build will not terminate an unrelated process."
            }
        }
    }
}

Write-StopStep "Revision: $StopRevision"

if (Test-RuntimeStopped) {
    Write-StopStep "Market AI runtime is already stopped."
} else {
    Assert-NoUnexpectedRuntimePortOwners

    $suiteProcesses = @(Get-OwnedProcesses "InvestmentLocalSuite")
    if ($suiteProcesses.Count -gt 0) {
        # This is the exact tray-menu command path for "서버·Bridge 종료".
        # The current and legacy Local Suite tray wndproc both map WM_COMMAND/1004
        # to _request_exit(stop_efriend=False), so eFriend Expert is preserved.
        Write-StopStep "Requesting Local Suite tray action: 서버·Bridge 종료 (eFriend 유지)..."
        $posted = Send-WindowMessageToProcesses "InvestmentLocalSuite" ([uint32]$WmCommand) $LocalSuiteExitKeepEfriendCommand
        if ($posted -gt 0) {
            Write-StopStep "Tray shutdown command posted to $posted Local Suite window(s)."
            if (Wait-RuntimeStopped $GraceSeconds) {
                Write-StopStep "Tray shutdown lifecycle completed."
            } else {
                Write-Host "[WARN] Tray shutdown did not clear the complete runtime within $GraceSeconds seconds. Applying expected-process fallback only." -ForegroundColor Yellow
            }
        } else {
            Write-Host "[WARN] Local Suite process exists but its tray window did not accept the shutdown command. Applying expected-process fallback only." -ForegroundColor Yellow
        }
    }

    if (-not (Test-RuntimeStopped)) {
        Assert-NoUnexpectedRuntimePortOwners

        # Preserve the same logical boundary as the tray command: server first,
        # Bridge next, Local Suite last. Never target eFriend Expert processes.
        Stop-ExpectedProcess "MarketAI"

        if (@(Get-OwnedProcesses "KisKospi200Bridge").Count -gt 0) {
            Write-StopStep "Requesting KIS Bridge graceful exit message..."
            $bridgePosted = Send-WindowMessageToProcesses "KisKospi200Bridge" ([uint32]$BridgeExitMessage) 0
            if ($bridgePosted -gt 0) {
                $bridgeDeadline = (Get-Date).AddSeconds(5)
                while ((Get-Date) -lt $bridgeDeadline -and (@(Get-OwnedProcesses "KisKospi200Bridge").Count -gt 0)) {
                    Start-Sleep -Milliseconds 250
                }
            }
        }
        Stop-ExpectedProcess "KisKospi200Bridge"
        Stop-ExpectedProcess "InvestmentLocalSuite"
    }

    Assert-NoUnexpectedRuntimePortOwners
    if (-not (Wait-RuntimeStopped 5)) {
        Fail "Runtime shutdown verification failed. MarketAI/Bridge/Local Suite or ports 8000-8002 are still active."
    }
}

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host " MARKET AI RUNTIME STOP : SUCCESS" -ForegroundColor Green
Write-Host "==============================================================" -ForegroundColor Green
Write-Host " Target      : $RuntimeRoot"
Write-Host " Local Suite : STOPPED / NOT RUNNING"
Write-Host " MarketAI    : STOPPED / NOT RUNNING"
Write-Host " KIS Bridge  : STOPPED / NOT RUNNING"
Write-Host " Ports       : 8000 / 8001 / 8002 FREE"
Write-Host " eFriend     : PRESERVED (never targeted)"
Write-Host "==============================================================" -ForegroundColor Green
