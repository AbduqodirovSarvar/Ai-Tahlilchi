"""Sports the system knows about."""

from enum import StrEnum


class Sport(StrEnum):
    """A sport; each one is served by its own :class:`~app.sports.base.SportAdapter`.

    Only football is implemented in v1; the others are declared so that schema, settings
    and user-facing filters do not change when an adapter for them is added.
    """

    FOOTBALL = "football"
    HOCKEY = "hockey"
    BASKETBALL = "basketball"
    VOLLEYBALL = "volleyball"
    TENNIS = "tennis"
