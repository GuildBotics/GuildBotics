"""What runs only inside a command's isolated environment.

The entry the host starts in the command's microVM (:mod:`.entry`), the
working directory's copy (:mod:`.worktree_copy`), the AI CLI tool adapters
and their turns, the AI CLI brain (:mod:`.cli_agent`), and the client of the
command's window to the host (:mod:`.host_client`, :mod:`.window`). It
imports the shared packages and nothing of the host's: the host starts it by
module name and reads only the wire
(:mod:`guildbotics.intelligences.agent_runtime.wire`).
"""
