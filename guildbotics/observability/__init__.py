"""The host's writers of diagnostics and activity records.

The correlation every record carries (traces and spans) is shared with what
runs inside a command's isolated environment, so it lives in
:mod:`guildbotics.utils.correlation`; this package only writes and reads the
records on the host.
"""
