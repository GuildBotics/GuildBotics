"""The isolated agent environment GuildBotics runs every AI CLI turn in.

The access contract (:mod:`.contract`) says what a turn may reach; the
environment enforces it, the same way on every OS and for
every provider, by running the provider CLI inside a microVM that GuildBotics
creates for the turn and discards afterwards. :mod:`.spec` turns the contract
into the microVM's mounts, network policy, working directory, and environment
without touching any runtime; :mod:`.runtime` is the one module that drives
the microsandbox SDK.
"""
