# Windows PowerShell 5.1. No application runtime is needed.
<#
.SYNOPSIS
Install, replace, inspect or remove a per-user OpenSSH forwarding task.
.DESCRIPTION
Connect with ssh first to accept the host key. Use an unencrypted key or the
Windows OpenSSH agent. SSH configuration and keys are never modified. The task
runs at login, without a visible console, and retries ssh after 15 seconds.
.EXAMPLE
powershell -ExecutionPolicy Bypass -File hub-forward.ps1 -Action install -Hub my-hub -Port "12345,23456"
.EXAMPLE
powershell -ExecutionPolicy Bypass -File hub-forward.ps1 -Action status
.EXAMPLE
powershell -ExecutionPolicy Bypass -File hub-forward.ps1 -Action uninstall
#>
[CmdletBinding()]
param(
    [ValidateSet('install', 'uninstall', 'status')]
    [string]$Action = 'status',
    [string]$Hub,
    # -File in Windows PowerShell 5.1 cannot pass array-valued parameters.
    [string]$Port
)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = 'hub-forward-' + $identity.User.Value
$shell = Join-Path $PSHOME 'powershell.exe'

if ($Action -eq 'status') {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) { Write-Output 'Not installed'; return }
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    [pscustomobject]@{
        TaskName = $task.TaskName; State = $task.State
        LastRunTime = $info.LastRunTime; LastTaskResult = $info.LastTaskResult
    }
    return
}
if ($Action -eq 'install') {
    if (-not $Hub -or $Hub -notmatch '^[a-zA-Z0-9_.@:%+\[\]-]+$' -or $Hub.StartsWith('-')) {
        throw 'Use an SSH config alias or [user@]host as -Hub.'
    }
    if (-not $Port) { throw 'Pass a comma-separated list of ports with -Port.' }
    $ports = @($Port.Split(',') | ForEach-Object {
        if ($_ -notmatch '^[1-9][0-9]{0,4}$' -or [int]$_ -gt 65535) {
            throw 'Each port must be an integer from 1 to 65535.'
        }
        [int]$_
    } | Select-Object -Unique)
    $ssh = (Get-Command ssh.exe -CommandType Application).Source
    $sshArgs = @('-N', '-T', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=3', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'ControlMaster=no', '-o', 'ControlPath=none')
    foreach ($p in $ports) { $sshArgs += @('-L', "127.0.0.1:${p}:127.0.0.1:${p}") }
    $sshArgs += @('--', $Hub)
}
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    # Stopping the task ends only the process it started, so the loop and its
    # ssh would outlive it. Collect the whole tree, parents first.
    $processes = @(Get-CimInstance Win32_Process)
    $tree = @($processes | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($existing.Actions[0].Arguments) })
    $layer = $tree
    while ($layer) {
        $layer = @($processes | Where-Object { $layer.ProcessId -contains $_.ParentProcessId })
        $tree += $layer
    }
    Stop-ScheduledTask -TaskName $taskName
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    # The loop goes before its ssh, so that it starts no ssh after them.
    $tree | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
if ($Action -eq 'uninstall') { return }
# Encode arguments as PowerShell literals, without evaluating any user text.
function Quote-Literal([string]$value) { return "'" + $value.Replace("'", "''") + "'" }
$arguments = ($sshArgs | ForEach-Object { Quote-Literal $_ }) -join ', '
$source = @"
`$ErrorActionPreference = 'Stop'
`$sshArgs = @($arguments)
while (`$true) {
    try { & $(Quote-Literal $ssh) @sshArgs }
    catch { [Console]::Error.WriteLine(`$_.Exception.Message) }
    Start-Sleep -Seconds 15
}
"@
# The loop travels in the task itself: a file written here may be invisible to
# the task, as AppData writes of a process in a packaged (MSIX) app are.
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($source))
# -WindowStyle Hidden still shows the console as PowerShell starts; a headless
# console has no window to show.
$conhost = Join-Path $env:windir 'System32\conhost.exe'
$taskAction = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$shell`" -NoProfile -NonInteractive -EncodedCommand $encoded"
$user = $identity.Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output 'Installed hub-forward. Use -Action status to inspect the task.'
