"""How a member's operation fails short of a service."""


class MemberCapabilityError(RuntimeError):
    """A member's operation failed in the capability itself: what was asked
    is not one it performs, or its own work (the member's git) failed.

    A service's failures are :class:`~guildbotics.runtime.port.ServiceError`.
    The message is safe to show the member.
    """
