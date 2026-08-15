"""Workspace storage: identity, shared-record schemas, and config revisions.

This package owns what a GuildBotics workspace *is* on disk once its
``config/`` and ``state/`` directories are shared between the user's machines:
the workspace and device identities, the schemas shared records must satisfy,
and the compare-and-set that keeps a stale editor from overwriting a newer
config. It sits below the capability, driver, and API layers and above
``guildbotics.utils``, so nothing here knows about providers or screens.
"""
