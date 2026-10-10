"""The isolated environment a command and its AI CLI turns run in, as the host
holds it.

The access contract (:mod:`.contract`) says what a turn may reach; the
environment enforces it, the same way on every OS and for every provider, by
running the command inside a microVM that GuildBotics boots for it and discards
afterwards. :mod:`.spec` turns the contract into the microVM's mounts, network
policy, working directory, and environment without touching any runtime;
:mod:`.runtime` is the one module that drives the microsandbox SDK, and
:mod:`.snapshot` builds what the microVM boots from. :mod:`.command_environment`
opens a command's microVM; the host answers what it asks through the command's
window (:mod:`.host_window`, served beside the member broker
:mod:`.member_broker`), lends logins through the credential gateway
(:mod:`.auth_gateway`, :mod:`.credential_vault`), makes the external inference
calls (:mod:`.inference_host`), keeps the conversation ledger
(:mod:`.store`), records the turns (:mod:`.diagnostics`, :mod:`.span_summary`),
and writes back what a command changed in a copy of its working directory
(:mod:`.worktree`).
"""
