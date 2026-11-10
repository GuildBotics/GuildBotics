"""Run Windows PowerShell 5.1 with ScheduledTask cmdlets replaced by doubles."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1"),
]
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/hub-forward.ps1"
HARNESS = r"""
$ErrorActionPreference = 'Stop'
$global:HFexists = $false
$global:HFcalls = @()
function Get-ScheduledTask {
    param($TaskName, $ErrorAction)
    if ($global:HFexists) {
        $action = [pscustomobject]@{ Arguments=$global:HFregistration.Action.Argument }
        [pscustomobject]@{ TaskName=$TaskName; State='Running'; Actions=@($action) }
    }
}
function New-HFProcess($id, $parent, $command, $created) {
    [pscustomobject]@{ ProcessId=$id; ParentProcessId=$parent; CommandLine=$command; CreationDate=$created }
}
# Each registration runs the task's console (101), its loop (102) and ssh
# (103), listed children first, beside another PowerShell with its ssh, and a
# process older than 103 still naming its long-gone parent by 103's ID (108).
function Reset-HFProcesses {
    $earlier = (Get-Date).AddMinutes(-1)
    $argument = $global:HFregistration.Action.Argument
    $loop = 'powershell.exe -NoProfile -NonInteractive ' + $argument.Substring($argument.IndexOf('-EncodedCommand'))
    $global:HFspawned = $false
    $global:HFprocesses = @(
        (New-HFProcess 103 102 'ssh.exe' $earlier),
        (New-HFProcess 102 101 $loop $earlier),
        (New-HFProcess 101 1 ('conhost.exe ' + $argument) $earlier),
        (New-HFProcess 104 1 $null $earlier),
        (New-HFProcess 105 104 'ssh.exe' $earlier),
        (New-HFProcess 108 103 'explorer.exe' $earlier.AddMinutes(-1)))
}
function Get-CimInstance {
    param($ClassName)
    $global:HFprocesses
    if (-not $global:HFspawned) {
        # The loop starts another ssh (106) right after the first listing; a
        # later process reuses the loop's ID as its parent (107).
        $global:HFspawned = $true
        $global:HFprocesses += New-HFProcess 106 102 'ssh.exe' (Get-Date).AddSeconds(-1)
        $global:HFprocesses += New-HFProcess 107 102 'reused' (Get-Date).AddMinutes(1)
    }
}
function Stop-Process {
    param($Id, [switch]$Force, $ErrorAction)
    $global:HFcalls += "kill $Id"
    $global:HFprocesses = @($global:HFprocesses | Where-Object ProcessId -ne $Id)
}
function Wait-Process { param($Id, $Timeout, $ErrorAction) $global:HFcalls += 'wait' }
function Stop-ScheduledTask {
    param($TaskName)
    $global:HFcalls += 'stop'
    # Ends the console it started (101), leaving the loop running.
    $global:HFprocesses = @($global:HFprocesses | Where-Object ProcessId -ne 101)
}
function Unregister-ScheduledTask {
    param($TaskName, [switch]$Confirm)
    $global:HFcalls += 'unregister'; $global:HFexists = $false
}
function New-ScheduledTaskAction {
    param($Execute, $Argument)
    [pscustomobject]$PSBoundParameters
}
function New-ScheduledTaskTrigger { param([switch]$AtLogOn, $User) [pscustomobject]$PSBoundParameters }
function New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel) [pscustomobject]$PSBoundParameters }
function New-ScheduledTaskSettingsSet {
    param($ExecutionTimeLimit, $MultipleInstances, [switch]$AllowStartIfOnBatteries,
        [switch]$DontStopIfGoingOnBatteries, $RestartCount, $RestartInterval, [switch]$StartWhenAvailable)
    [pscustomobject]$PSBoundParameters
}
function Register-ScheduledTask {
    param($TaskName, $Action, $Trigger, $Principal, $Settings, [switch]$Force)
    $global:HFregistration = [pscustomobject]$PSBoundParameters
    $global:HFexists = $true; $global:HFcalls += 'register'
    Reset-HFProcesses
}
function Start-ScheduledTask { param($TaskName) $global:HFcalls += 'start' }
function Get-ScheduledTaskInfo { param($TaskName) $global:HFcalls += 'status'; [pscustomobject]@{LastTaskResult=0} }
function Get-NetTCPConnection {
    param($State, $ErrorAction)
    [pscustomobject]@{LocalAddress='127.0.0.1'; LocalPort=12345}
    [pscustomobject]@{LocalAddress='192.0.2.1'; LocalPort=23456}
}
function Get-Loop {
    $encoded = [regex]::Match($global:HFregistration.Action.Argument, '-EncodedCommand (\S+)$').Groups[1].Value
    [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded))
}
function Get-Command { param($Name, $CommandType) [pscustomobject]@{Source=$env:HF_SSH} }
function Start-Sleep {
    param($Seconds)
    if ($Seconds -ne 15) { throw 'wrong restart interval' }
    $global:HFiterations++
    if ($global:HFiterations -eq 2) { throw 'end-loop' }
}
"""


def run_powershell(tmp_path, body):
    harness = tmp_path / "harness.ps1"
    harness.write_text(HARNESS + body, encoding="utf-8-sig")
    stub = tmp_path / "ssh space ' 東京.ps1"
    stub.write_text(
        "ConvertTo-Json -InputObject @($args) -Compress | "
        "Add-Content -LiteralPath $env:HF_LOG -Encoding UTF8\n"
        '1..8 | ForEach-Object { Write-Error "Permission denied $_" -ErrorAction Continue }\n'
        "exit 255\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
        ],
        env={
            **os.environ,
            "LOCALAPPDATA": str(tmp_path / "local space 東京"),
            "USERPROFILE": str(tmp_path / "profile space ' 東京"),
            "HF_SCRIPT": str(SCRIPT),
            "HF_SSH": str(stub),
            "HF_LOG": str(tmp_path / "ssh.jsonl"),
        },
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return json.loads((tmp_path / "result.json").read_text(encoding="utf-8-sig"))


def test_install_update_status_remove_and_retry(tmp_path):
    result = run_powershell(
        tmp_path,
        r"""
& $env:HF_SCRIPT -Action install -Hub 'user@hub' -Port '12345,23456,12345' | Out-Null
$initial = $global:HFregistration
& $env:HF_SCRIPT -Action install -Hub 'user@hub' -Port '12345,23456' | Out-Null
if ($initial.Action.Argument -ne $global:HFregistration.Action.Argument) { throw 'not idempotent' }
$global:HFiterations = 0
try { . ([scriptblock]::Create((Get-Loop))) } catch { if ($_.Exception.Message -ne 'end-loop') { throw } }
$status = & $env:HF_SCRIPT -Action status
$statusText = $status | Out-String -Width 200
& $env:HF_SCRIPT -Action install -Hub 'other-hub' -Port 34567 | Out-Null
$updated = Get-Loop
$freshStatus = & $env:HF_SCRIPT -Action status
& $env:HF_SCRIPT -Action uninstall
& $env:HF_SCRIPT -Action uninstall
# A file the task read could be hidden from it (a packaged app's AppData).
$written = Test-Path -LiteralPath $env:LOCALAPPDATA
@{initial=$initial; updated=$updated; calls=$global:HFcalls; written=$written; status=$status; statusText=$statusText; freshStatus=$freshStatus} |
    ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 (Join-Path $PSScriptRoot 'result.json')
""",
    )
    initial = result["initial"]
    assert initial["TaskName"].startswith("hub-forward-S-1-")
    # A headless console: -WindowStyle Hidden still shows one as it starts.
    assert initial["Action"]["Execute"].lower().endswith("\\system32\\conhost.exe")
    argument = initial["Action"]["Argument"]
    assert argument.startswith('--headless "')
    assert 'powershell.exe" -NoProfile -NonInteractive -EncodedCommand ' in argument
    assert "-File" not in argument
    assert initial["Trigger"]["AtLogOn"]["IsPresent"]
    assert initial["Principal"]["LogonType"] == "Interactive"
    assert initial["Principal"]["RunLevel"] == "Limited"
    assert initial["Settings"]["ExecutionTimeLimit"]["Ticks"] == 0
    assert initial["Settings"]["MultipleInstances"] == "IgnoreNew"
    assert initial["Settings"]["RestartInterval"]["TotalSeconds"] == 60
    # Replacing and removing end what the task left, found by its loop even
    # after its console ended, a generation at a time, each gone before its
    # children are listed: the ssh started after the first listing ends too,
    # while processes naming a reused ID and the other PowerShell's do not.
    # Once removed, there is nothing left to stop.
    kills = ["kill 102", "wait", "kill 103", "kill 106", "wait"]
    replace = ["stop", "unregister", *kills, "register", "start"]
    assert result["calls"] == [
        "register",
        "start",
        *replace,
        "status",
        *replace,
        "status",
        "stop",
        "unregister",
        *kills,
    ]
    assert not result["written"]
    status = result["status"]
    assert status["State"] == "Running"
    assert status["Ports"].splitlines() == [
        "12345: Listening=True",
        "23456: Listening=False",
    ]
    assert status["LastSshExitCode"] == 255
    assert status["LastSshEndedAt"]
    assert status["LastSshStderr"].splitlines() == [
        f"Permission denied {i}" for i in range(4, 9)
    ]
    assert "Permission denied 8" in result["statusText"]
    assert "23456: Listening=False" in result["statusText"]
    assert status["DiagnosticPath"].endswith("last-exit.json")
    assert result["freshStatus"]["LastSshEndedAt"] is None
    assert "No SSH exit recorded yet" in result["freshStatus"]["DiagnosticStatus"]
    assert result["freshStatus"]["Ports"] == "34567: Listening=False"
    assert "23456" not in result["updated"]
    assert "34567" in result["updated"]
    lines = (tmp_path / "ssh.jsonl").read_text(encoding="utf-8-sig").splitlines()
    assert (
        len(lines) == 2
    )  # A failed ssh restarts too, without relying on Task Scheduler.
    expected_args = [
        "-N",
        "-T",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-L",
        "127.0.0.1:12345:127.0.0.1:12345",
        "-L",
        "127.0.0.1:23456:127.0.0.1:23456",
        "--",
        "user@hub",
    ]
    assert [json.loads(line) for line in lines] == [expected_args, expected_args]


@pytest.mark.parametrize("ports", [[12345], [12345, 23456, 12345]])
def test_status_reads_existing_task_without_state_files(tmp_path, ports):
    forwards = ", ".join(f"'-L', '127.0.0.1:{p}:127.0.0.1:{p}'" for p in ports)
    result = run_powershell(
        tmp_path,
        r"""
# Model an already registered task without invoking this version's install.
$source = @'
$ErrorActionPreference = 'Stop'
$sshArgs = @('-N', FORWARDS, '--', 'hub')
throw 'status must not execute the task source'
while ($true) { & 'ssh.exe' @sshArgs; Start-Sleep -Seconds 15 }
'@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($source))
$global:HFregistration = [pscustomobject]@{
    Action=[pscustomobject]@{Argument="--headless powershell.exe -NoProfile -NonInteractive -EncodedCommand $encoded"}
}
$global:HFexists = $true
$status = & $env:HF_SCRIPT -Action status
@{status=$status; written=(Test-Path -LiteralPath $env:USERPROFILE); calls=$global:HFcalls} |
    ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 (Join-Path $PSScriptRoot 'result.json')
""".replace("FORWARDS", forwards),
    )
    assert result["status"]["State"] == "Running"
    assert result["status"]["Ports"].splitlines() == [
        f"{p}: Listening={p == 12345}" for p in dict.fromkeys(ports)
    ]
    assert "No SSH exit recorded yet" in result["status"]["DiagnosticStatus"]
    assert "installed again" in result["status"]["DiagnosticStatus"]
    assert result["status"]["LastSshEndedAt"] is None
    assert not result["written"]
    assert result["calls"] == ["status"]


def test_status_with_task_without_embedded_ports(tmp_path):
    result = run_powershell(
        tmp_path,
        r"""
$global:HFregistration = [pscustomobject]@{
    Action=[pscustomobject]@{Argument='-NoProfile -File "missing runner.ps1"'}
}
$global:HFexists = $true
$status = & $env:HF_SCRIPT -Action status
$status | ConvertTo-Json -Depth 10 |
    Set-Content -Encoding UTF8 (Join-Path $PSScriptRoot 'result.json')
""",
    )
    assert result["State"] == "Running"
    assert "Unknown ports" in result["Ports"]
    assert "Install the task again" in result["Ports"]
    assert "No SSH exit recorded yet" in result["DiagnosticStatus"]


def test_native_stderr_is_retained_without_stopping_retries(tmp_path):
    result = run_powershell(
        tmp_path,
        r"""
$native = Join-Path $PSScriptRoot 'ssh native.cmd'
@('@echo off', 'for /L %%i in (1,1,8) do echo Permission denied %%i 1>&2', 'exit /b 255') |
    Set-Content -LiteralPath $native -Encoding ASCII
$env:HF_SSH = $native
& $env:HF_SCRIPT -Action install -Hub hub -Port 12345 | Out-Null
$global:HFiterations = 0
try { . ([scriptblock]::Create((Get-Loop))) } catch { if ($_.Exception.Message -ne 'end-loop') { throw } }
$status = & $env:HF_SCRIPT -Action status
@{status=$status; iterations=$global:HFiterations} | ConvertTo-Json -Depth 10 |
    Set-Content -Encoding UTF8 (Join-Path $PSScriptRoot 'result.json')
""",
    )
    assert result["iterations"] == 2
    exit_record = result["status"]
    assert exit_record["LastSshExitCode"] == 255
    assert exit_record["LastSshEndedAt"]
    assert [line.strip() for line in exit_record["LastSshStderr"].splitlines()] == [
        f"Permission denied {i}" for i in range(4, 9)
    ]


@pytest.mark.parametrize(
    "args",
    [[], ["-N"], ["-N", "a space", "'東京\\", "$literal%"]],
)
def test_ssh_double_records_an_argument_array(tmp_path, args):
    # Exercise the recorder independently of the generated retry loop. Neither
    # an empty nor a single-element argument vector may collapse to a scalar.
    literals = ", ".join("'" + arg.replace("'", "''") + "'" for arg in args)
    run_powershell(
        tmp_path,
        f"""
$values = @({literals})
& $env:HF_SSH @values
@{{}} | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $PSScriptRoot 'result.json')
""",
    )
    lines = (tmp_path / "ssh.jsonl").read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == args


@pytest.mark.parametrize(
    "options",
    [
        "-Hub '-oProxyCommand=bad' -Port 1",
        "-Hub 'hub;bad' -Port 1",
        "-Hub hub -Port 0",
        "-Hub hub -Port 65536",
        "-Hub hub",
        "-Port 1",
    ],
)
def test_invalid_input_preserves_task(tmp_path, options):
    result = run_powershell(
        tmp_path,
        r"""
& $env:HF_SCRIPT -Action install -Hub hub -Port 12345 | Out-Null
$global:HFcalls = @()
$failed = $false
try { & $env:HF_SCRIPT -Action install OPTIONS | Out-Null } catch { $failed = $true }
@{failed=$failed; calls=@($global:HFcalls)} | ConvertTo-Json |
    Set-Content -Encoding UTF8 (Join-Path $PSScriptRoot 'result.json')
""".replace("OPTIONS", options),
    )
    assert result == {"failed": True, "calls": []}
