"""Which build is running: version and git commit, for the startup log and ``/holat``.

The image build bakes ``APP_VERSION`` and ``GIT_SHA`` into the environment (Dockerfile build
args); a source checkout falls back to the installed package version and ``unknown``.
"""

from dataclasses import dataclass
from importlib import metadata
from typing import Final

from app.config import Settings, get_settings

PACKAGE_NAME: Final = "ai-tahlilchi"
SHORT_SHA_CHARS: Final = 12
UNKNOWN: Final = "unknown"


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """The running build."""

    version: str
    git_sha: str

    @property
    def label(self) -> str:
        """One-line form for status messages: ``0.6.1 (a1b2c3d4e5f6)``."""
        return f"{self.version} ({self.git_sha})"


def _installed_version() -> str:
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return UNKNOWN


def get_version(settings: Settings | None = None) -> VersionInfo:
    """The version of this build; ``settings`` defaults to the process settings."""
    settings = settings or get_settings()
    version = (settings.app_version or "").strip().removeprefix("v") or _installed_version()
    sha = settings.git_sha.strip()[:SHORT_SHA_CHARS] or UNKNOWN
    return VersionInfo(version=version, git_sha=sha)
