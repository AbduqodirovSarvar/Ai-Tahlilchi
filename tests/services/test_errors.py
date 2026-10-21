from datetime import timedelta

import pytest

from app.errors import AppError
from app.services.errors import (
    BudgetExceededError,
    CooldownActiveError,
    NotFoundError,
    ServiceError,
    ValidationFailedError,
)


@pytest.mark.parametrize(
    "error",
    [
        ValidationFailedError("min_ev", "too high"),
        NotFoundError("signal", 7),
        CooldownActiveError(timedelta(minutes=3)),
        BudgetExceededError(needed=12, available=4),
    ],
)
def test_every_service_error_is_an_app_error(error: ServiceError) -> None:
    assert isinstance(error, AppError)
    assert str(error)


def test_validation_failed_names_the_field_and_is_a_value_error() -> None:
    error = ValidationFailedError("min_probability", "must be within [0.3, 0.95]")

    assert isinstance(error, ValueError)
    assert (error.field, error.message) == ("min_probability", "must be within [0.3, 0.95]")
    assert str(error) == "min_probability: must be within [0.3, 0.95]"


def test_not_found_keeps_the_lookup() -> None:
    error = NotFoundError("signal", 42)

    assert isinstance(error, LookupError)
    assert (error.entity, error.entity_id) == ("signal", 42)


def test_cooldown_and_budget_carry_structured_data() -> None:
    assert CooldownActiveError(timedelta(seconds=90)).retry_after == timedelta(seconds=90)
    budget = BudgetExceededError(needed=12, available=4)
    assert (budget.needed, budget.available) == (12, 4)
