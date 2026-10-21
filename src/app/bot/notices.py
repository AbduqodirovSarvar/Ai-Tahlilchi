"""One-time notices such as the §6.6 high-probability warning ("bir marta ko'rsatadi").

Kept in process memory: after a restart the warning may be shown once more, which is
harmless. Persisting "already warned" needs a settings field (requested from the service
contract owner, see ADR 0006).
"""

HIGH_PROBABILITY: str = "high_probability"


class OnceNotices:
    """Remembers which notice each user has already seen."""

    def __init__(self) -> None:
        """Start with nobody warned."""
        self._seen: set[tuple[int, str]] = set()

    def first_time(self, user_id: int, notice: str) -> bool:
        """True exactly once per user and notice."""
        key = (user_id, notice)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True
