"""Exceptions raised by the statistical models.

They extend the project hierarchy (:mod:`app.errors`) so the pipeline can tell "this input
cannot be modelled" (skip the fixture, log it) apart from programming bugs.
"""

from app.errors import AppError, DataError


class ModelInputError(DataError):
    """Input to a model is invalid: odds <= 1, probability outside [0, 1], bad line, etc."""


class UnknownTeamError(ModelInputError):
    """A prediction was requested for a team the fitted model has never seen."""


class ModelFitError(AppError):
    """A model could not be fitted: the optimiser did not converge or data is degenerate."""
