"""Usage probes talk to fake provider processes and reap them on every exit."""

import os
import sys
from pathlib import Path

import pytest

from guildbotics.guest import usage_probe


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import json,sys
for line in sys.stdin:
    item=json.loads(line)
    if 'id' not in item:continue
    if item['method']=='initialize':result={}
    elif item['method']=='account/rateLimits/read':result={'rateLimits':{'primary':{'usedPercent':12,'windowDurationMins':300}}}
    else:raise RuntimeError('unexpected request')
    print(json.dumps({'jsonrpc':'2.0','id':item['id'],'result':result}),flush=True)
"""
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    return executable


@pytest.mark.asyncio
async def test_codex_usage_uses_account_rpc_and_returns_normalized_snapshot(fake_cli):
    snapshot = await usage_probe.read_codex_usage()
    assert snapshot.agent == "codex"
    assert snapshot.windows[0].used_percent == 12
    assert snapshot.windows[0].window_minutes == 300


@pytest.mark.asyncio
async def test_probe_timeout_reaps_process(fake_cli, monkeypatch):
    executable: Path = fake_cli
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    captured = []
    original = usage_probe._probe

    async def probe(*args):
        process = await original(*args)
        captured.append(process)
        return process

    monkeypatch.setattr(usage_probe, "_probe", probe)
    with pytest.raises(usage_probe.CliAgentUsageError, match="did not answer in time"):
        await usage_probe.read_codex_usage(timeout=0.1)
    assert captured[0].returncode is not None
