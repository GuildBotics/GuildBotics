"""The host's writers of diagnostics and activity records.

Work targets use ``target.*`` and chats use ``chat.*`` attributes. Domain
events name their subject and occurrence, independently of the provider.
The correlation every record carries (traces and spans) is shared with what
runs inside a command's isolated environment, so it lives in
:mod:`guildbotics.utils.correlation`; this package only writes and reads the
records on the host.
"""
