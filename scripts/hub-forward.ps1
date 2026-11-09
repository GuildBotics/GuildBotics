# Windows PowerShell 5.1. No application runtime is needed.
<#
.SYNOPSIS
Install, replace, inspect or remove a per-user OpenSSH forwarding task.
.DESCRIPTION
Use an SSH config alias or user@host as -Hub. Connect with ssh first to accept
the host key. Use an unencrypted key or the
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
# Keep diagnostics outside AppData, which packaged apps may virtualize.
$stateDir = Join-Path $env:USERPROFILE '.hub-forward'
$configPath = Join-Path $stateDir 'ports.json'
$exitPath = Join-Path $stateDir 'last-exit.json'

if ($Action -eq 'status') {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) { Write-Output 'Not installed'; return }
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    $ports = @(Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json)
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop)
    $lastExit = if (Test-Path -LiteralPath $exitPath) {
        Get-Content -LiteralPath $exitPath -Raw | ConvertFrom-Json
    } else { $null }
    [pscustomobject]@{
        TaskName = $task.TaskName; State = $task.State
        LastRunTime = $info.LastRunTime; LastTaskResult = $info.LastTaskResult
        Ports = ($ports | ForEach-Object {
            $p = $_
            $listening = [bool]($listeners | Where-Object {
                $_.LocalPort -eq $p -and $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::')
            })
            "${p}: Listening=$listening"
        }) -join [Environment]::NewLine
        LastSshEndedAt = $lastExit.EndedAt; LastSshExitCode = $lastExit.ExitCode
        LastSshStderr = $lastExit.Stderr -join [Environment]::NewLine
        DiagnosticPath = $exitPath
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
    Stop-ScheduledTask -TaskName $taskName
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    # Stopping the task ends only the process it started, so its loop and ssh
    # would outlive it. They end a generation at a time: each is gone before
    # its children are listed, so none can start a child after the list. The
    # last argument, the encoded loop, is in the console's and the loop's
    # command lines alike.
    $loop = ($existing.Actions[0].Arguments -split ' ')[-1]
    $generation = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($loop) })
    while ($generation) {
        $generation | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Wait-Process -Id $generation.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
        $ended = Get-Date
        # A child starts while its parent runs; any other process naming the
        # parent's ID names an earlier or later holder of that ID.
        $generation = @(Get-CimInstance Win32_Process | Where-Object {
            $child = $_
            $parent = $generation | Where-Object ProcessId -eq $child.ParentProcessId
            $parent -and $parent.CreationDate -le $child.CreationDate -and $child.CreationDate -lt $ended })
    }
}
if ($Action -eq 'uninstall') { return }
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
ConvertTo-Json -InputObject @($ports) | Set-Content -LiteralPath $configPath -Encoding UTF8
Remove-Item -LiteralPath $exitPath -Force -ErrorAction SilentlyContinue
# Encode arguments as PowerShell literals, without evaluating any user text.
function Quote-Literal([string]$value) { return "'" + $value.Replace("'", "''") + "'" }
$arguments = ($sshArgs | ForEach-Object { Quote-Literal $_ }) -join ', '
$source = @"
`$ErrorActionPreference = 'Stop'
`$sshArgs = @($arguments)
`$exitPath = $(Quote-Literal $exitPath)
while (`$true) {
    `$tail = New-Object 'System.Collections.Generic.Queue[string]'
    `$code = `$null
    try {
        # PowerShell 5.1 wraps native stderr in ErrorRecord objects. Continue
        # lets ssh finish, retaining its exit code even when it writes stderr.
        `$ErrorActionPreference = 'Continue'
        & $(Quote-Literal $ssh) @sshArgs 2>&1 | ForEach-Object {
            if (`$_ -is [System.Management.Automation.ErrorRecord]) {
                `$line = `$_.ToString()
                `$tail.Enqueue(`$line.Substring(0, [Math]::Min(2000, `$line.Length)))
                if (`$tail.Count -gt 5) { `$null = `$tail.Dequeue() }
            }
        }
        `$code = `$LASTEXITCODE
    }
    catch {
        `$tail.Enqueue(`$_.Exception.Message)
        if (`$tail.Count -gt 5) { `$null = `$tail.Dequeue() }
    }
    finally { `$ErrorActionPreference = 'Stop' }
    # Replace one bounded record, so status never reads a partially written JSON.
    [pscustomobject]@{
        EndedAt = [DateTimeOffset]::Now.ToString('o'); ExitCode = `$code
        Stderr = @(`$tail.ToArray())
    } | ConvertTo-Json | Set-Content -LiteralPath (`$exitPath + '.tmp') -Encoding UTF8
    Move-Item -LiteralPath (`$exitPath + '.tmp') -Destination `$exitPath -Force
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
Write-Output 'Use an SSH config alias or user@host as -Hub. Connect with ssh first to accept the host key.'
