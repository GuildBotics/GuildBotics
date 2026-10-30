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
    if ($global:HFexists) { [pscustomobject]@{ TaskName=$TaskName; State='Running' } }
}
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
        "$args | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:HF_LOG\n"
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
& $env:HF_SCRIPT -Action install -Hub 'user@hub' -Port '12345,23456,12345' | Out-Null
$initial = $global:HFregistration
$runner = Join-Path $env:LOCALAPPDATA 'hub-forward/run.ps1'
$first = [IO.File]::ReadAllText($runner)
& $env:HF_SCRIPT -Action install -Hub 'user@hub' -Port '12345,23456' | Out-Null
if ($first -ne [IO.File]::ReadAllText($runner)) { throw 'not idempotent' }
$global:HFiterations = 0
try { . $runner } catch { if ($_.Exception.Message -ne 'end-loop') { throw } }
& $env:HF_SCRIPT -Action install -Hub 'other-hub' -Port 34567 | Out-Null
$updated = [IO.File]::ReadAllText($runner)
& $env:HF_SCRIPT -Action status | Out-Null
& $env:HF_SCRIPT -Action uninstall
& $env:HF_SCRIPT -Action uninstall
@{initial=$initial; updated=$updated; calls=$global:HFcalls; remains=(Test-Path $runner)} |
    ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 (Join-Path $PSScriptRoot 'result.json')
""",
    )
    initial = result["initial"]
    assert initial["TaskName"].startswith("hub-forward-S-1-")
    assert "powershell.exe" in initial["Action"]["Execute"]
    assert "-WindowStyle Hidden" in initial["Action"]["Argument"]
    assert "-NoProfile -NonInteractive" in initial["Action"]["Argument"]
    assert '-ExecutionPolicy Bypass -File "' in initial["Action"]["Argument"]
    assert initial["Trigger"]["AtLogOn"]["IsPresent"]
    assert initial["Principal"]["LogonType"] == "Interactive"
    assert initial["Principal"]["RunLevel"] == "Limited"
    assert initial["Settings"]["ExecutionTimeLimit"]["Ticks"] == 0
    assert initial["Settings"]["MultipleInstances"] == "IgnoreNew"
    assert initial["Settings"]["RestartInterval"]["TotalSeconds"] == 60
    calls = result["calls"]
    assert calls[:6] == ["register", "start", "stop", "unregister", "register", "start"]
    assert calls[-3:] == ["status", "stop", "unregister"]
    assert not result["remains"]
    assert "23456" not in result["updated"]
    assert "34567" in result["updated"]
    lines = (tmp_path / "ssh.jsonl").read_text().splitlines()
    assert (
        len(lines) == 2
    )  # A failed ssh restarts too, without relying on Task Scheduler.
    args = json.loads(lines[0])
    assert args == json.loads(lines[1])
    assert args.count("-N") == 1
    assert args[-6:] == [
        "-L",
        "127.0.0.1:12345:127.0.0.1:12345",
        "-L",
        "127.0.0.1:23456:127.0.0.1:23456",
        "--",
        "user@hub",
    ]
    for option in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "ExitOnForwardFailure=yes",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=3",
    ):
        assert option in args


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
