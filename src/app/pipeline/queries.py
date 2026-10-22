"""Compatibility re-exports: the read helpers moved to :mod:`app.db.repo.catalog` (ADR 0011).

Kept so existing imports keep working; new code imports from ``app.db.repo``.
"""

from app.db.repo.catalog import (
    get_league,
    get_team,
    league_refs,
    league_teams,
    list_leagues,
    team_refs,
    user_ids,
)

__all__ = [
    "get_league",
    "get_team",
    "league_refs",
    "league_teams",
    "list_leagues",
    "team_refs",
    "user_ids",
]
