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
# The task's console (101), its loop (102) and ssh (103), listed children
# first, and another PowerShell with its ssh.
function Get-CimInstance {
    param($ClassName)
    $task = 'conhost.exe ' + $global:HFregistration.Action.Argument
    [pscustomobject]@{ ProcessId=103; ParentProcessId=102; CommandLine='ssh.exe' }
    [pscustomobject]@{ ProcessId=102; ParentProcessId=101; CommandLine='powershell.exe' }
    [pscustomobject]@{ ProcessId=101; ParentProcessId=1; CommandLine=$task }
    [pscustomobject]@{ ProcessId=104; ParentProcessId=1; CommandLine=$null }
    [pscustomobject]@{ ProcessId=105; ParentProcessId=104; CommandLine='ssh.exe' }
}
function Stop-Process { param($Id, [switch]$Force, $ErrorAction) $global:HFcalls += "kill $Id" }
function Stop-ScheduledTask { param($TaskName) $global:HFcalls += 'stop' }
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
}
function Start-ScheduledTask { param($TaskName) $global:HFcalls += 'start' }
function Get-ScheduledTaskInfo { param($TaskName) $global:HFcalls += 'status'; [pscustomobject]@{LastTaskResult=0} }
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
function Get-Loop {
    $encoded = [regex]::Match($global:HFregistration.Action.Argument, '-EncodedCommand (\S+)$').Groups[1].Value
    [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded))
}
& $env:HF_SCRIPT -Action install -Hub 'user@hub' -Port '12345,23456,12345' | Out-Null
$initial = $global:HFregistration
& $env:HF_SCRIPT -Action install -Hub 'user@hub' -Port '12345,23456' | Out-Null
if ($initial.Action.Argument -ne $global:HFregistration.Action.Argument) { throw 'not idempotent' }
$global:HFiterations = 0
try { . ([scriptblock]::Create((Get-Loop))) } catch { if ($_.Exception.Message -ne 'end-loop') { throw } }
& $env:HF_SCRIPT -Action install -Hub 'other-hub' -Port 34567 | Out-Null
$updated = Get-Loop
& $env:HF_SCRIPT -Action status | Out-Null
& $env:HF_SCRIPT -Action uninstall
& $env:HF_SCRIPT -Action uninstall
# A file the task read could be hidden from it (a packaged app's AppData).
$written = Test-Path -LiteralPath $env:LOCALAPPDATA
@{initial=$initial; updated=$updated; calls=$global:HFcalls; written=$written} |
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
    # Replacing and removing end the task's tree, parents first, and nothing
    # else; once removed, there is nothing left to stop.
    kills = ["kill 101", "kill 102", "kill 103"]
    replace = ["stop", "unregister", *kills, "register", "start"]
    assert result["calls"] == [
        "register",
        "start",
        *replace,
        *replace,
        "status",
        "stop",
        "unregister",
        *kills,
    ]
    assert not result["written"]
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
