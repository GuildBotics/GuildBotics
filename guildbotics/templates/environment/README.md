# Snapshot recipe data

`recipe.json` holds the base image, uv and Python versions, guest paths,
Node.js version and archive digests, architecture names, and native PDF
packages. The Python host reads these values while it remains on main;
the Rust recipe will consume this data in #87 after the Python host is removed.
The provider CLI pins remain in `intelligences/cli_agents.py`.

`requirements.txt` is exported from `uv.lock`. Regenerate it after changing
the lock, then run `tests/test_environment_requirements.py`:

```bash
uv export --frozen --no-dev --no-emit-project --no-header --no-annotate \
  --no-hashes --prune microsandbox --prune wakepy \
  -o guildbotics/templates/environment/requirements.txt
```

The exclusions apply while the Python host dependencies remain in the lock.
#122 removes unused host dependencies and updates the export with them.

For production updates before the Rust switch (#97), use the production
checkout and its own update procedure. The recipe implementation and its
validation on main will be updated in #87, #95, and #96.
