# Snapshot recipe data

`recipe.json` holds the base image, uv and Python versions, guest paths,
Node.js version and archive digests, architecture names, and native PDF
packages. The Rust recipe in `gb-environment` consumes this data.
The provider CLI pins remain in `intelligences/cli_agents.py`; regenerate
their Rust-readable projection after changing them:

```bash
uv run --no-sync python scripts/generate-environment-catalog.py
```

`tests/scripts/test_environment_catalog.py` checks that the projection stays
current. `records/golden/snapshot_recipe.json` records the frozen Python
host's scripts and names with the current guest requirements as input; the
Rust recipe tests compare each script and the resulting digest to it.

The recipe keeps these choices separate from the host implementation:

| Key | Reason |
| --- | --- |
| `image` | The default is an exact Debian-based image tag, so devices build from the same base. Custom images must provide apt, curl, tar, gcc, g++, and make to build Python dependencies without wheels for `python_version`, and git for members' clones inside the environment. |
| `uv_version` | The default image has no Python of its own, so uv is installed from its release archive and provides the Python toolchain. |
| `node_version` | The provider CLIs run on this Node.js, installed over the image's version. Raising Node.js changes the recipe without changing the base image. |
| `node_sha256` | The digests from Node.js's `SHASUMS256.txt` verify each release archive before unpacking it. Keys are the architecture names returned by `uname -m`. |
| `node_platforms` | Maps those machine names to the Linux archive names (`linux-x64`, `linux-arm64`). |
| `python_version` | GuildBotics' own code runs on this Python. An image that already provides it is used as is; otherwise uv installs it under `/opt/uv/python`, outside the home directory that turns bind over. |
| `venv` | Holds the guest's Python and dependencies. Its `.pth` file alone makes the mounted GuildBotics code importable; the turn's environment does not point other processes at that code. |
| `code_root` | Mounts the running process's own package read-only at a GuildBotics path instead of its host path, so it cannot cover a user's working directory, including a turn working on GuildBotics itself. |
| `pdf_packages` | Provides WeasyPrint's native libraries and Latin/CJK fonts for `to_pdf`; without the CJK fonts, Japanese text renders as empty boxes. |

`requirements.txt` is exported from `uv.lock`. Regenerate it after changing
the lock, then run `tests/test_environment_requirements.py`:

```bash
uv export --frozen --no-dev --no-emit-project --no-header --no-annotate \
  --no-hashes \
  -o guildbotics/templates/environment/requirements.txt
```

The exported content is part of a build step, so a change changes the
snapshot's name and causes a rebuild.

For production updates before the Rust switch (#97), use the production
checkout and its own update procedure.
