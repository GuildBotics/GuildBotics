"""#115: the record endpoints already implemented by the Rust server.

Set GUILDBOTICS_BLACKBOX_SERVER to "/path/to/guildbotics server start".
The production golden directory stays outside the repository.
"""

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.blackbox.harness import block_runtime, serving

SOURCE = os.environ.get("GUILDBOTICS_BLACKBOX_GOLDEN", "")


@pytest.mark.skipif(not SOURCE, reason="No offline production golden copy supplied")
@pytest.mark.timeout(1200)
def test_existing_record_endpoints_match_golden(tmp_path):
    source = Path(SOURCE)
    workspace, home = tmp_path / "workspace", tmp_path / "home"
    shutil.copytree(source / "workspace", workspace)
    shutil.copytree(source / "identity", home / ".guildbotics/identity")
    block_runtime(home)
    golden = source / "golden"
    with serving(tmp_path, workspace, home, NO_PROXY="") as stack:
        response = stack.api.get("diagnostics/traces", params={"limit": 1000})
        assert response.status_code == 200
        paths = ["diagnostics/traces"] + [
            f"diagnostics/traces/{trace['trace_id']}"
            for trace in response.json()["traces"]
        ]

        def differs(path):
            response = stack.api.get(path, params={"limit": 1000}, timeout=120)
            assert response.status_code == 200
            actual = json.loads(
                response.text.replace(json.dumps(str(tmp_path))[1:-1], "<root>")
            )
            expected = json.loads(
                (golden / (path.replace("/", "__") + ".json")).read_text()
            )
            return path if actual != expected else None

        with ThreadPoolExecutor(max_workers=4) as workers:
            differing = [path for path in workers.map(differs, paths) if path]
        # Never include production record bodies in failure output.
        assert not differing, f"{len(differing)} record endpoints differ: {differing}"
