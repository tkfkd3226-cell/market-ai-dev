param(
    [string]$TrayName = 'e-Friend Expert',
    [string]$ExitName = '종료',
    [int]$TimeoutSeconds = 6
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class TrayExitNative {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll")]
    public static extern IntPtr GetDlgItem(IntPtr hDlg, int nIDDlgItem);

    [DllImport("user32.dll")]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int X, int Y);

    [DllImport("user32.dll")]
    public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
}
"@

$MOUSEEVENTF_LEFTDOWN  = 0x0002
$MOUSEEVENTF_LEFTUP    = 0x0004
$MOUSEEVENTF_RIGHTDOWN = 0x0008
$MOUSEEVENTF_RIGHTUP   = 0x0010

function Get-ClassName {
    param([IntPtr]$Hwnd)
    if ($Hwnd -eq [IntPtr]::Zero) { return '' }
    $sb = New-Object System.Text.StringBuilder 256
    [void][TrayExitNative]::GetClassName($Hwnd, $sb, $sb.Capacity)
    return $sb.ToString()
}

function Get-TopWindows {
    $result = New-Object System.Collections.Generic.List[System.IntPtr]
    $callback = [TrayExitNative+EnumWindowsProc]{
        param([IntPtr]$hWnd, [IntPtr]$lParam)
        $result.Add($hWnd)
        return $true
    }
    [void][TrayExitNative]::EnumWindows($callback, [IntPtr]::Zero)
    return $result.ToArray()
}

function Get-AutomationElement {
    param([IntPtr]$Hwnd)
    if ($Hwnd -eq [IntPtr]::Zero) { return $null }
    try {
        return [System.Windows.Automation.AutomationElement]::FromHandle($Hwnd)
    } catch {
        return $null
    }
}

function Find-ExactNameInRoot {
    param(
        [Parameter(Mandatory=$true)]$Root,
        [Parameter(Mandatory=$true)][string]$Name,
        [string]$ControlTypeName = ''
    )

    try {
        $condition = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            $Name
        )
        $items = $Root.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            $condition
        )
        foreach ($item in $items) {
            try {
                if ($item.Current.IsOffscreen) { continue }
                $rect = $item.Current.BoundingRectangle
                if ($rect.Width -le 0 -or $rect.Height -le 0) { continue }
                if ($ControlTypeName) {
                    if ($item.Current.ControlType.ProgrammaticName -ne $ControlTypeName) { continue }
                }
                return $item
            } catch {
                continue
            }
        }
    } catch {
        return $null
    }
    return $null
}

function Click-Element {
    param(
        [Parameter(Mandatory=$true)]$Element,
        [switch]$Right
    )

    $rect = $Element.Current.BoundingRectangle
    if ($rect.Width -le 0 -or $rect.Height -le 0) {
        throw 'Target element has no visible bounding rectangle.'
    }

    $x = [int][Math]::Round($rect.Left + ($rect.Width / 2.0))
    $y = [int][Math]::Round($rect.Top  + ($rect.Height / 2.0))
    if (-not [TrayExitNative]::SetCursorPos($x, $y)) {
        throw "SetCursorPos failed for $x,$y"
    }
    Start-Sleep -Milliseconds 120

    if ($Right) {
        [TrayExitNative]::mouse_event($MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, [UIntPtr]::Zero)
        [TrayExitNative]::mouse_event($MOUSEEVENTF_RIGHTUP,   0, 0, 0, [UIntPtr]::Zero)
    } else {
        [TrayExitNative]::mouse_event($MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero)
        [TrayExitNative]::mouse_event($MOUSEEVENTF_LEFTUP,   0, 0, 0, [UIntPtr]::Zero)
    }
}

function Find-TrayElement {
    param([string]$Name)

    # Search only Explorer's taskbar/notification roots. Do not traverse the
    # complete desktop UIA tree; some providers can block for many seconds.
    $candidateHandles = New-Object System.Collections.Generic.List[System.IntPtr]
    $shellTray = [TrayExitNative]::FindWindow('Shell_TrayWnd', $null)
    if ($shellTray -ne [IntPtr]::Zero) { $candidateHandles.Add($shellTray) }

    foreach ($hwnd in (Get-TopWindows)) {
        $className = Get-ClassName $hwnd
        if ($className -match 'NotifyIconOverflowWindow|TopLevelWindowForOverflowXamlIsland|Overflow|TrayNotify|Xaml') {
            if (-not $candidateHandles.Contains($hwnd)) { $candidateHandles.Add($hwnd) }
        }
    }

    foreach ($hwnd in $candidateHandles) {
        $root = Get-AutomationElement $hwnd
        if ($null -eq $root) { continue }
        $item = Find-ExactNameInRoot -Root $root -Name $Name
        if ($null -ne $item) { return $item }
    }

    # The icon may live in Windows 11's hidden-icons flyout. Open that flyout
    # through the taskbar UI and repeat the bounded search.
    if ($shellTray -ne [IntPtr]::Zero) {
        $taskbarRoot = Get-AutomationElement $shellTray
        if ($null -ne $taskbarRoot) {
            $showHidden = $null
            foreach ($label in @('숨겨진 아이콘 표시', 'Show hidden icons')) {
                $showHidden = Find-ExactNameInRoot -Root $taskbarRoot -Name $label
                if ($null -ne $showHidden) { break }
            }
            if ($null -ne $showHidden) {
                try {
                    $invoke = $showHidden.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
                    if ($null -ne $invoke) { $invoke.Invoke() } else { Click-Element -Element $showHidden }
                } catch {
                    Click-Element -Element $showHidden
                }
                Start-Sleep -Milliseconds 400

                foreach ($hwnd in (Get-TopWindows)) {
                    $className = Get-ClassName $hwnd
                    if ($className -match 'NotifyIconOverflowWindow|TopLevelWindowForOverflowXamlIsland|Overflow|TrayNotify|Xaml') {
                        $root = Get-AutomationElement $hwnd
                        if ($null -eq $root) { continue }
                        $item = Find-ExactNameInRoot -Root $root -Name $Name
                        if ($null -ne $item) { return $item }
                    }
                }
            }
        }
    }

    return $null
}

function Hide-HiddenTrayFlyout {
    # If the helper had to open Windows 11's hidden-icons flyout to reach
    # e-Friend Expert, close that flyout again so shutdown leaves no tray UI open.
    foreach ($hwnd in (Get-TopWindows)) {
        $className = Get-ClassName $hwnd
        if ($className -eq 'NotifyIconOverflowWindow' -or
            $className -eq 'TopLevelWindowForOverflowXamlIsland') {
            try { [void][TrayExitNative]::ShowWindow($hwnd, 0) } catch { }
        }
    }
}

function Find-EFriendExitConfirmDialog {
    # The verified final shutdown dialog is an efexpertmain.exe-owned visible
    # #32770 dialog with Button CtrlId 1 (종료) and CtrlId 2 (취소).
    $mainPids = @(
        Get-Process -Name 'efexpertmain' -ErrorAction SilentlyContinue |
            ForEach-Object { [uint32]$_.Id }
    )
    if ($mainPids.Count -eq 0) { return $null }

    foreach ($hwnd in (Get-TopWindows)) {
        if (-not [TrayExitNative]::IsWindowVisible($hwnd)) { continue }
        if ((Get-ClassName $hwnd) -ne '#32770') { continue }

        [uint32]$ownerPid = 0
        [void][TrayExitNative]::GetWindowThreadProcessId($hwnd, [ref]$ownerPid)
        if ($mainPids -notcontains $ownerPid) { continue }

        $exitButton = [TrayExitNative]::GetDlgItem($hwnd, 1)
        $cancelButton = [TrayExitNative]::GetDlgItem($hwnd, 2)
        if ($exitButton -eq [IntPtr]::Zero -or $cancelButton -eq [IntPtr]::Zero) { continue }
        if ((Get-ClassName $exitButton) -ne 'Button') { continue }
        if ((Get-ClassName $cancelButton) -ne 'Button') { continue }

        return [PSCustomObject]@{
            DialogHwnd = $hwnd
            ExitHwnd   = $exitButton
            CancelHwnd = $cancelButton
            ProcessId  = $ownerPid
        }
    }

    return $null
}

function Test-EFriendProcessesStopped {
    return -not [bool](
        Get-Process -Name 'efexpertmain','xexpertgate','efriendexpert' -ErrorAction SilentlyContinue
    )
}

$tray = Find-TrayElement -Name $TrayName
if ($null -eq $tray) {
    Write-Output "TRAY_NOT_FOUND:$TrayName"
    Hide-HiddenTrayFlyout
    exit 2
}

# e-Friend Expert uses a custom tray popup that does not expose its items
# reliably through UI Automation or a standard Win32 HMENU.  On the actual
# program, opening the tray menu and pressing Down three times selects the
# final "종료" item; Enter then performs the program's own clean shutdown.
Click-Element -Element $tray -Right
Start-Sleep -Milliseconds 300

try {
    [System.Windows.Forms.SendKeys]::SendWait('{DOWN 3}')
    Start-Sleep -Milliseconds 120
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
} catch {
    [System.Windows.Forms.SendKeys]::SendWait('{ESC}') 2>$null
    Write-Output "EXIT_KEY_SEQUENCE_FAILED:$($_.Exception.GetType().Name)"
    Hide-HiddenTrayFlyout
    exit 3
}

# eFriend does not exit immediately after selecting tray > 종료.  It shows a
# dedicated confirmation dialog.  The real dialog has been observed as:
#   owner process: efexpertmain.exe
#   class:         #32770
#   CtrlId 1:      종료 (Button)
#   CtrlId 2:      취소 (Button)
# Find that exact native dialog and invoke CtrlId 1 directly, without relying
# on screen coordinates, dialog text, or a second keyboard focus assumption.
$deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(2, $TimeoutSeconds))
$confirm = $null
while ([DateTime]::UtcNow -lt $deadline) {
    $confirm = Find-EFriendExitConfirmDialog
    if ($null -ne $confirm) { break }
    if (Test-EFriendProcessesStopped) {
        Write-Output 'EXIT_CONFIRMED'
        Hide-HiddenTrayFlyout
        exit 0
    }
    Start-Sleep -Milliseconds 100
}

if ($null -eq $confirm) {
    Write-Output 'EXIT_CONFIRM_NOT_FOUND'
    Hide-HiddenTrayFlyout
    exit 4
}

$BM_CLICK = 0x00F5
[void][TrayExitNative]::SendMessage(
    $confirm.ExitHwnd,
    $BM_CLICK,
    [IntPtr]::Zero,
    [IntPtr]::Zero
)

while ([DateTime]::UtcNow -lt $deadline) {
    if (Test-EFriendProcessesStopped) {
        Write-Output 'EXIT_CONFIRMED'
        Hide-HiddenTrayFlyout
        exit 0
    }
    Start-Sleep -Milliseconds 100
}

Write-Output 'EXIT_CONFIRM_CLICKED_BUT_RUNNING'
Hide-HiddenTrayFlyout
exit 5
