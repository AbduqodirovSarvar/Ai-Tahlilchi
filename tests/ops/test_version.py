"""Which build runs: baked build args win, the installed package is the fallback."""

from app.config import Settings
from app.ops.version import UNKNOWN, get_version


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg, arg-type]


def test_build_args_are_reported() -> None:
    info = get_version(settings(app_version="v0.6.1", git_sha="0123456789abcdef0123"))
    assert info.version == "0.6.1"
    assert info.git_sha == "0123456789ab"
    assert info.label == "0.6.1 (0123456789ab)"


def test_a_source_checkout_falls_back_to_the_package_version() -> None:
    info = get_version(settings())
    assert info.version not in {"", UNKNOWN}
    assert info.git_sha == UNKNOWN


def test_blank_values_are_treated_as_missing() -> None:
    info = get_version(settings(app_version="  ", git_sha="  "))
    assert info.version != ""
    assert info.git_sha == UNKNOWN
