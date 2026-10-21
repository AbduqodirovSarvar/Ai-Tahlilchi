"""Versioned prompt templates, shipped as package data next to this module.

A prompt is a file ``<name>.v<N>.md``. Changing wording means adding ``v<N+1>`` (never
editing a released version in place), so every stored result can name the exact prompt that
produced it and rolling back is a one-constant change in ``service.py``.
"""

import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Final

from app.errors import ConfigurationError

_NAME: Final = re.compile(r"^[a-z_]+$")
_VERSION: Final = re.compile(r"^v\d+$")


@dataclass(frozen=True, slots=True)
class Prompt:
    """A loaded prompt and the identifier recorded with every result."""

    name: str
    version: str
    text: str

    @property
    def version_id(self) -> str:
        """Identifier stored with results, e.g. ``context_check.v1``."""
        return f"{self.name}.{self.version}"


@cache
def load_prompt(name: str, version: str) -> Prompt:
    """Read a prompt once per process.

    Raises:
        ConfigurationError: the name/version is malformed or the file is missing or empty.
    """
    if not _NAME.fullmatch(name) or not _VERSION.fullmatch(version):
        msg = f"invalid prompt reference {name!r} {version!r}"
        raise ConfigurationError(msg)
    resource = resources.files(__package__).joinpath(f"{name}.{version}.md")
    try:
        text = resource.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        msg = f"prompt {name}.{version} not found"
        raise ConfigurationError(msg) from exc
    if not text:
        msg = f"prompt {name}.{version} is empty"
        raise ConfigurationError(msg)
    return Prompt(name=name, version=version, text=text)
