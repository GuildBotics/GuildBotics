# Execute the real installer and decoded supervisor without registering tasks.
$ErrorActionPreference = 'Stop'
$global:sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$global:registered = $null
$global:killed = @()
$global:processes = @()
$global:spawned = $false
$global:stops = 0
function Assert($condition) { if (-not $condition) { throw 'runner contract failed' } }
function New-TestProcess($id, $parent, $command, $created, $owner) {
    [pscustomobject]@{ ProcessId=$id; ParentProcessId=$parent; CommandLine=$command; CreationDate=$created; Owner=$owner }
}
function Get-ScheduledTask {
    param($TaskName, $ErrorAction)
    $global:existing
}
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($Filter) {
        $id = [int]($Filter -replace 'ProcessId = ', '')
        $global:processes | Where-Object ProcessId -eq $id
    } else {
        $global:processes
        if (-not $global:spawned -and $global:processes) {
            # A child starts just after the snapshot, before the supervisor stops.
            $global:spawned = $true
            $global:processes += New-TestProcess 14 12 'new job' (Get-Date).AddSeconds(-1) $global:sid
        }
    }
}
function Invoke-CimMethod {
    param($InputObject, $MethodName, $ErrorAction)
    Assert ($MethodName -eq 'GetOwnerSid')
    Assert (@($global:processes | Where-Object {
        $_.ProcessId -eq $InputObject.ProcessId -and $_.CreationDate -eq $InputObject.CreationDate
    }).Count -eq 1)
    [pscustomobject]@{ Sid=$InputObject.Owner }
}
function Stop-ScheduledTask {
    param($TaskName)
    $global:stops++
    # A directly started legacy PowerShell disappears here. Its runner survives.
    $global:processes = @($global:processes | Where-Object { $_.ProcessId -ne 10 -and (-not $global:vanish -or $_.ProcessId -ne 12) })
    if ($global:reuse) {
        $global:processes = @($global:processes | Where-Object ProcessId -ne 12)
        $global:processes += New-TestProcess 12 1 'reused runner PID' (Get-Date).AddMilliseconds(-500) $global:sid
        $global:processes += New-TestProcess 99 12 'unrelated child of reused PID' (Get-Date).AddMilliseconds(-100) $global:sid
    }
}
function Get-Process {
    param($Id, $ErrorAction)
    if (@($global:processes | Where-Object ProcessId -eq $Id).Count -eq 0) { return }
    if ($global:unseen -and $Id -eq 12) {
        # The observed parent is gone before a handle can be opened. A new
        # holder of its PID creates a child and exits between snapshots.
        $global:processes = @($global:processes | Where-Object ProcessId -ne 12)
        $global:processes += New-TestProcess 99 12 'unobserved reused parent child' (Get-Date).AddMilliseconds(-100) $global:sid
        return
    }
    $created = ($global:processes | Where-Object ProcessId -eq $Id).CreationDate
    $handle = if ($global:denied -and $Id -eq 12) { 0 } else { 47 }
    $target = [pscustomobject]@{ Id=[int]$Id; Handle=[IntPtr]$handle; Created=$created }
    $target | Add-Member -MemberType ScriptProperty -Name HasExited -Value {
        @($global:processes | Where-Object { $_.ProcessId -eq $this.Id -and $_.CreationDate -eq $this.Created }).Count -eq 0
    }
    $target | Add-Member -MemberType ScriptMethod -Name Kill -Value {
        $global:processes = @($global:processes | Where-Object ProcessId -ne $this.Id)
        if ($global:race -and $this.Id -eq 12) { throw 'exited naturally before kill' }
        $global:killed += $this.Id
    }
    $target | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($timeout) $true }
    $target | Add-Member -MemberType ScriptMethod -Name Dispose -Value { }
    $target
}
function New-ScheduledTaskAction { param($Execute, $Argument) [pscustomobject]$PSBoundParameters }
function New-ScheduledTaskTrigger { param([switch]$AtLogOn, $User) [pscustomobject]$PSBoundParameters }
function New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel) [pscustomobject]$PSBoundParameters }
function New-ScheduledTaskSettingsSet {
    param($ExecutionTimeLimit, $RestartCount, $RestartInterval, $MultipleInstances,
        [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries, [switch]$StartWhenAvailable)
    [pscustomobject]$PSBoundParameters
}
function Register-ScheduledTask {
    param($TaskName, $Action, $Trigger, $Principal, $Settings, [switch]$Force)
    $global:registered = [pscustomobject]$PSBoundParameters
}
function Start-ScheduledTask { param($TaskName) Assert ($TaskName -eq $global:registered.TaskName) }

$global:existing = $null
& $env:RUNNER_TEST_INSTALLER
$registration = $global:registered
Assert ($registration.TaskName -eq ('guildbotics-runner-' + $global:sid + '-7878'))
Assert ($registration.Action.Execute.EndsWith('conhost.exe'))
Assert ($registration.Action.Argument.StartsWith('--headless '))
Assert ($registration.Principal.RunLevel -eq 'Limited')
Assert ($registration.Principal.LogonType -eq 'Interactive')
Assert ($registration.Trigger.User -eq $global:sid)
Assert ($registration.Settings.StartWhenAvailable)
Assert ($registration.Settings.MultipleInstances -eq 'IgnoreNew')
Assert ($registration.Settings.ExecutionTimeLimit.TotalSeconds -eq 0)

# Replace both previously published direct-PowerShell and headless actions.
foreach ($mode in @('direct', 'console', 'middle-exits', 'exits-before-kill', 'reused-parent', 'unseen-parent')) {
    $legacy = $mode -ne 'console'
    $global:vanish = $mode -eq 'middle-exits'
    $global:race = $mode -eq 'exits-before-kill'
    $global:reuse = $mode -eq 'reused-parent'
    $global:unseen = $mode -eq 'unseen-parent'
    $arguments = if ($legacy) { '-NoProfile -NonInteractive -EncodedCommand Zml4dHVyZQ==' } else { $registration.Action.Argument }
    $global:existing = [pscustomobject]@{ Actions=@([pscustomobject]@{ Arguments=$arguments }) }
    $earlier = (Get-Date).AddMinutes(-1)
    $global:processes = @(
        (New-TestProcess 10 1 ('console ' + $arguments) $earlier $global:sid),
        (New-TestProcess 12 $(if ($legacy) { 10 } else { 11 }) 'runner' $earlier $global:sid),
        (New-TestProcess 13 12 'job' $earlier $global:sid),
        (New-TestProcess 20 1 ('other user ' + $arguments) $earlier 'other-sid'),
        (New-TestProcess 21 20 'other runner' $earlier 'other-sid'),
        (New-TestProcess 22 12 'old reused parent' $earlier.AddMinutes(-1) $global:sid),
        (New-TestProcess 23 12 'future reused parent' (Get-Date).AddMinutes(1) $global:sid))
    if (-not $legacy) { $global:processes += New-TestProcess 11 10 ('powershell ' + $arguments) $earlier $global:sid }
    $global:killed = @(); $global:spawned = $false
    & $env:RUNNER_TEST_INSTALLER
    Assert ($global:killed -contains 13)
    Assert (($global:killed -contains 14) -eq (-not $global:unseen))
    Assert (($global:killed -contains 12) -eq (-not ($global:vanish -or $global:race -or $global:reuse -or $global:unseen)))
    Assert (($global:killed -contains 11) -eq (-not $legacy))
    Assert (($global:killed | Where-Object { $_ -in @(20, 21, 22, 23, 99) }).Count -eq 0)
}
# Failure to pin an owned process must abort before stopping the working task.
$global:unseen = $false; $global:reuse = $false; $global:vanish = $false; $global:race = $false
$global:denied = $true
$global:existing = [pscustomobject]@{ Actions=@([pscustomobject]@{ Arguments=$registration.Action.Argument }) }
$earlier = (Get-Date).AddMinutes(-1)
$global:processes = @(
    (New-TestProcess 10 1 ('console ' + $registration.Action.Argument) $earlier $global:sid),
    (New-TestProcess 12 10 'runner' $earlier $global:sid))
$before = $global:stops
$refused = $false
try { & $env:RUNNER_TEST_INSTALLER } catch { $refused = $true }
Assert $refused
Assert ($global:stops -eq $before)
Assert ($null -ne $global:existing)
$global:denied = $false
# Malformed task arguments must never become an empty/broad process match.
$global:existing = [pscustomobject]@{ Actions=@([pscustomobject]@{ Arguments='-EncodedCommand ' }) }
$global:killed = @()
& $env:RUNNER_TEST_INSTALLER
Assert ($global:killed.Count -eq 0)

$encoded = [regex]::Match($registration.Action.Argument, '-EncodedCommand (\S+)$').Groups[1].Value
$loop = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded))
$global:iterations = 0
function Start-Sleep {
    param($Seconds)
    Assert ($Seconds -eq 15)
    $global:iterations++
    # First exit is 0, second is 19, third is a spawn error. All retry.
    if ($global:iterations -eq 2) { Remove-Item -LiteralPath $env:RUNNER_TEST_STUB }
    if ($global:iterations -eq 3) { throw 'end-loop' }
}
try { & ([scriptblock]::Create($loop)); throw 'supervisor ended' }
catch { Assert ($_.Exception.Message -eq 'end-loop') }
Assert ($global:iterations -eq 3)
