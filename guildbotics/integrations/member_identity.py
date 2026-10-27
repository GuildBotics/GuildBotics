"""A member's public names and how a provider addresses them.

Providers construct this value without credentials. Reads keep the service's
spelling; matching it against these names resolves a service actor to a member.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MemberIdentity:
    names: tuple[str, ...] = ()
    mention: str = ""

    @property
    def name(self) -> str:
        """The name patrol snapshots use for this member."""
        return self.names[0] if self.names else ""

    def matches(self, name: str) -> bool:
        """Whether a nonempty service name denotes this member."""
        return bool(name) and name.casefold() in {
            candidate.casefold() for candidate in self.names
        }
