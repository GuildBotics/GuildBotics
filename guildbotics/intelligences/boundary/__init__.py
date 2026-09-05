"""The boundary GuildBotics puts around every AI CLI turn.

The sandbox contract (:mod:`guildbotics.intelligences.sandbox`) says what a
turn may reach; this package enforces it, the same way on every OS and for
every provider, by running the provider CLI inside a microVM that GuildBotics
creates for the turn and discards afterwards. :mod:`.spec` turns the contract
into the microVM's mounts, network policy, working directory, and environment
without touching any runtime; :mod:`.runtime` is the one module that drives
the microsandbox SDK.
"""
