# Windows PowerShell 5.1; both placeholders are filled by service_script.
$port = 0
$encoded = ''
$ErrorActionPreference = 'Stop'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$name = 'guildbotics-runner-' + $sid + '-' + $port
# Keep process handles until the whole tree is drained: Windows cannot reuse
# their IDs while a handle is open, even after the process has exited.
$handles = @{}
function Get-RunnerProcess($process) {
    $key = "$($process.ProcessId):$($process.CreationDate.Ticks)"
    if ($handles.ContainsKey($key)) { return $handles[$key] }
    $target = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
    if (-not $target) { return }
    try {
        $handle = $target.Handle
        if (-not $handle -or $handle -eq [IntPtr]::Zero) { throw [UnauthorizedAccessException]::new() }
        $live = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ProcessId)"
        if ($live -and $live.CreationDate -eq $process.CreationDate -and
            (Invoke-CimMethod -InputObject $live -MethodName GetOwnerSid).Sid -eq $sid) {
            $handles[$key] = $target
            return $target
        }
    }
    catch { if (-not $target.HasExited) { throw } }
    finally { if (-not $handles.ContainsKey($key)) { $target.Dispose() } }
}
try {
$existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($existing) {
    $generation = @()
    if ($existing.Actions[0].Arguments -match '-EncodedCommand ([A-Za-z0-9+/=]+)$') {
        $pattern = '-EncodedCommand ' + [regex]::Escape($Matches[1]) + '$'
        # Snapshot before stopping: older tasks directly started PowerShell,
        # whose runner child loses its identifiable parent when the task stops.
        $observed = @(Get-CimInstance Win32_Process)
        $generation = @($observed | Where-Object {
            $_.CommandLine -and $_.CommandLine -match $pattern -and
                (Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid -ErrorAction SilentlyContinue).Sid -eq $sid
        })
    }
    $initial = $observed
    # Open handles for the observed tree before task shutdown can erase its
    # intermediate processes. Descendants discovered later keep theirs too.
    $pending = $generation
    while ($pending) {
        foreach ($process in $pending) { $null = Get-RunnerProcess $process }
        $pending = @($initial | Where-Object {
            $child = $_
            $pending | Where-Object {
                $_.ProcessId -eq $child.ParentProcessId -and $_.CreationDate -le $child.CreationDate
            }
        })
    }
    Stop-ScheduledTask -TaskName $name
    # Keep its action registered until the replacement succeeds, so a retry
    # can still find descendants when stopping a process fails.
    while ($generation) {
        foreach ($process in $generation) {
            $target = Get-RunnerProcess $process
            if ($target -and -not $target.HasExited) {
                try {
                    $target.Kill()
                    if (-not $target.WaitForExit(10000)) { throw [TimeoutException]::new() }
                }
                catch { if (-not $target.HasExited) { throw } }
            }
        }
        $ended = Get-Date
        $observed = @($observed + @(Get-CimInstance Win32_Process) | Sort-Object ProcessId, CreationDate -Unique)
        # Finish one generation before listing children, including children
        # started after the first snapshot. Retain intermediate nodes that
        # exited before the next snapshot. Creation times exclude reused IDs.
        $generation = @($observed | Where-Object {
            $child = $_
            # The newest observed holder of a parent ID at the child's birth
            # owns that child. An earlier holder must never inherit its tree.
            $parent = $observed | Where-Object {
                $_.ProcessId -eq $child.ParentProcessId -and $_.CreationDate -le $child.CreationDate
            } | Sort-Object CreationDate -Descending | Select-Object -First 1
            # Without a held parent, only the original snapshot proves this
            # lineage. Do not guess about children born after it disappeared.
            $known = $handles.ContainsKey("$($parent.ProcessId):$($parent.CreationDate.Ticks)") -or
                ($initial | Where-Object { $_.ProcessId -eq $child.ProcessId -and $_.CreationDate -eq $child.CreationDate })
            $parent -and $known -and $child.CreationDate -lt $ended -and
                ($generation | Where-Object {
                    $_.ProcessId -eq $parent.ProcessId -and $_.CreationDate -eq $parent.CreationDate
                })
        })
    }
}
} finally { foreach ($target in $handles.Values) { $target.Dispose() } }
$shell = Join-Path $PSHOME 'powershell.exe'
$conhost = Join-Path $env:windir 'System32\conhost.exe'
$action = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$shell`" -NoProfile -NonInteractive -EncodedCommand $encoded"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid
$principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $name
