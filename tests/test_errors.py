"""Tests for app.errors."""

import pytest

from app.errors import AppError, ConfigurationError, DataError, ExternalServiceError


@pytest.mark.parametrize("error_type", [ConfigurationError, ExternalServiceError, DataError])
def test_project_errors_share_one_base(error_type: type[AppError]) -> None:
    with pytest.raises(AppError, match="boom"):
        raise error_type("boom")
