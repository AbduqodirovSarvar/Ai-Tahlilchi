"""Errors raised when a domain value object is constructed from invalid data."""

from app.errors import DataError


class DomainValidationError(DataError, ValueError):
    """A domain object would violate one of its invariants.

    Subclasses :class:`ValueError` too, so generic parsing code (e.g. ``pydantic``
    validators, CSV row parsers) can treat it like any other bad-value error.
    """
