"""T3 QA: cross-source team identity (no false merges) and Latin/Cyrillic team search."""

# Cyrillic test data is deliberate.
# ruff: noqa: RUF001

from collections.abc import Mapping

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import upsert_live
from app.db.search import Candidate, rank, search_key, search_teams, to_cyrillic, to_latin
from app.domain import ExternalRef, FixtureStatus, Sport, Team
from app.sports.team_matching import MatchMethod, TeamMatchReport, match_teams
from tests.domain.factories import KICKOFF, make_fixture


def team(source: str, name: str) -> Team:
    return Team(ref=ExternalRef(source, name), sport=Sport.FOOTBALL, name=name)


def report(provider: list[str], history: list[str], **options: object) -> TeamMatchReport:
    return match_teams(
        [team("api-football", name) for name in provider],
        [team("football-data", name) for name in history],
        **options,  # type: ignore[arg-type]
    )


def pairs(result: TeamMatchReport) -> dict[str, str]:
    return {match.provider.name: match.history.name for match in result.matched}


# --------------------------------------------------------------------------- aliases


def test_alias_hits_resolve_the_famous_spelling_differences() -> None:
    result = report(
        ["Manchester City", "Manchester United", "Real Betis", "Real Sociedad", "PEC Zwolle"],
        ["Man City", "Man United", "Betis", "Sociedad", "Zwolle", "PSV Eindhoven"],
    )
    assert pairs(result) == {
        "Manchester City": "Man City",
        "Manchester United": "Man United",
        "Real Betis": "Betis",
        "Real Sociedad": "Sociedad",
        "PEC Zwolle": "Zwolle",
    }
    assert {match.method for match in result.matched} == {MatchMethod.ALIAS}
    assert not result.ambiguous
    assert not result.unmatched


def test_alias_pointing_to_a_missing_history_team_is_not_guessed() -> None:
    result = report(["Manchester City"], ["Man United", "Newcastle"])
    assert pairs(result) == {}
    assert [t.name for t in result.unmatched] == ["Manchester City"]


# --------------------------------------------------------------------------- false merges

EPL = {
    "Manchester United": "Man United",
    "Manchester City": "Man City",
    "Newcastle": "Newcastle",
    "Nottingham Forest": "Nott'm Forest",
    "Tottenham": "Tottenham",
    "Wolves": "Wolves",
    "West Ham": "West Ham",
    "Brighton": "Brighton",
    "Brentford": "Brentford",
    "Bournemouth": "Bournemouth",
    "Everton": "Everton",
    "Fulham": "Fulham",
    "Crystal Palace": "Crystal Palace",
    "Aston Villa": "Aston Villa",
    "Arsenal": "Arsenal",
    "Chelsea": "Chelsea",
    "Liverpool": "Liverpool",
    "Leicester": "Leicester",
    "Ipswich": "Ipswich",
    "Southampton": "Southampton",
}
LA_LIGA = {
    "Real Madrid": "Real Madrid",
    "Barcelona": "Barcelona",
    "Atletico Madrid": "Ath Madrid",
    "Athletic Club": "Ath Bilbao",
    "Real Betis": "Betis",
    "Real Sociedad": "Sociedad",
    "Villarreal": "Villarreal",
    "Valencia": "Valencia",
    "Sevilla": "Sevilla",
    "Girona": "Girona",
    "Celta Vigo": "Celta",
    "Osasuna": "Osasuna",
    "Getafe": "Getafe",
    "Mallorca": "Mallorca",
    "Rayo Vallecano": "Vallecano",
    "Espanyol": "Espanol",
    "Alaves": "Alaves",
    "Las Palmas": "Las Palmas",
    "Leganes": "Leganes",
    "Real Valladolid": "Valladolid",
}
SERIE_A_NL = {
    "Inter": "Inter",
    "AC Milan": "Milan",
    "AS Roma": "Roma",
    "Juventus": "Juventus",
    "Napoli": "Napoli",
    "Lazio": "Lazio",
    "PSV": "PSV Eindhoven",
    "PEC Zwolle": "Zwolle",
    "Ajax": "Ajax",
    "Feyenoord": "Feyenoord",
}


@pytest.mark.parametrize("table", [EPL, LA_LIGA, SERIE_A_NL], ids=["epl", "la-liga", "serie-a-nl"])
@pytest.mark.parametrize("use_aliases", [True, False], ids=["aliases", "fuzzy-only"])
def test_full_league_tables_never_cross_merge(table: Mapping[str, str], use_aliases: bool) -> None:
    """Whatever the matcher decides, a confident match must be the right club."""
    options = {} if use_aliases else {"aliases": {}}
    result = report(list(table), list(table.values()), **options)
    wrong = {p: h for p, h in pairs(result).items() if table[p] != h}
    assert wrong == {}
    assert len({m.history.ref for m in result.matched}) == len(result.matched)


def test_aliased_tables_are_fully_matched() -> None:
    for table in (EPL, LA_LIGA, SERIE_A_NL):
        result = report(list(table), list(table.values()))
        assert pairs(result) == dict(table)


@pytest.mark.parametrize(
    ("provider", "history"),
    [
        (["Manchester City"], ["Man United", "Newcastle"]),
        (["Manchester United"], ["Man City", "Newcastle"]),
        (["Real Betis", "Real Sociedad"], ["Real Madrid", "Real Valladolid"]),
        (["Real Madrid"], ["Betis", "Sociedad", "Valladolid"]),
        (["Internazionale"], ["Internacional", "Milan"]),
        (["PSV"], ["PEC Zwolle", "Ajax"]),
        (["PEC Zwolle"], ["PSV Eindhoven", "Ajax"]),
        (["Atletico Madrid"], ["Real Madrid", "Ath Bilbao"]),
        (["Sheffield Wednesday"], ["Sheffield United", "Leeds"]),
    ],
)
def test_tricky_pairs_are_left_unmatched_when_the_real_club_is_absent(
    provider: list[str], history: list[str]
) -> None:
    result = report(provider, history, aliases={})
    assert pairs(result) == {}


def test_internazionale_is_ambiguous_not_silently_merged_with_internacional() -> None:
    result = report(["Internazionale"], ["Inter", "Internacional"])
    assert pairs(result) == {}
    # Scores 90 / 89 against Inter / Internacional: below the (raised) fuzzy threshold, so
    # the team is left for a human (alias) instead of being guessed.
    undecided = {t.name for t in result.unmatched} | {a.provider.name for a in result.ambiguous}
    assert undecided == {"Internazionale"}


def test_exact_inter_wins_over_internacional() -> None:
    assert pairs(report(["Inter"], ["Internacional", "Inter"])) == {"Inter": "Inter"}


@pytest.mark.parametrize(
    ("provider", "history"),
    [("Inter", ["Internacional", "Milan"]), ("Internacional", ["Inter", "Milan"])],
)
def test_prefix_names_are_not_merged_when_the_real_club_is_absent(
    provider: str, history: list[str]
) -> None:
    assert pairs(report([provider], history, aliases={})) == {}


@given(
    provider=st.lists(st.text(alphabet="abcde ÉéÖö'.-1", min_size=1, max_size=12), max_size=8),
    history=st.lists(st.text(alphabet="abcde ÉéÖö'.-1", min_size=1, max_size=12), max_size=8),
)
@settings(max_examples=150, deadline=None)
def test_matching_never_crashes_and_partitions_every_provider_team(
    provider: list[str], history: list[str]
) -> None:
    usable_provider = [name for name in provider if any(c.isalnum() for c in name)]
    usable_history = [name for name in history if any(c.isalnum() for c in name)]
    result = report(usable_provider, usable_history)
    outcome = (
        [m.provider.name for m in result.matched]
        + [a.provider.name for a in result.ambiguous]
        + [t.name for t in result.unmatched]
    )
    assert sorted(outcome) == sorted({name.strip() for name in usable_provider})
    claimed = [m.history.ref for m in result.matched]
    assert len(claimed) == len(set(claimed))  # nothing is claimed twice


# --------------------------------------------------------------------------- search

ARSENAL, CHELSEA = "Arsenal", "Chelsea"
NAMES = [
    "Arsenal",
    "Chelsea",
    "Liverpool",
    "Barcelona",
    "Real Madrid",
    "Atletico Madrid",
    "Manchester City",
    "Manchester United",
    "Juventus",
    "AC Milan",
    "Inter",
    "Internacional",
    "Bayern München",
    "Köln",
    "Borussia Dortmund",
    "Fenerbahçe",
    "Beşiktaş",
    "Galatasaray",
    "PSV Eindhoven",
    "Nott'm Forest",
]
CANDIDATES = [Candidate(index, name) for index, name in enumerate(NAMES)]


def top(query: str, limit: int = 3) -> list[str]:
    return [hit.name for hit in rank(query, CANDIDATES, limit=limit)]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Арсенал", "Arsenal"),
        ("Челси", "Chelsea"),
        ("Ливерпул", "Liverpool"),
        ("Барселона", "Barcelona"),
        ("Реал Мадрид", "Real Madrid"),
        ("Манчестер Сити", "Manchester City"),
        ("Манчестер Юнайтед", "Manchester United"),
        ("Ювентус", "Juventus"),
        ("Милан", "AC Milan"),
        ("Интер", "Inter"),
        ("Атлетико Мадрид", "Atletico Madrid"),
        ("Боруссия Дортмунд", "Borussia Dortmund"),
        ("Галатасарай", "Galatasaray"),
        ("Фенербахче", "Fenerbahçe"),
        ("Бешикташ", "Beşiktaş"),
        ("ПСВ", "PSV Eindhoven"),
    ],
)
def test_cyrillic_query_finds_the_latin_name(query: str, expected: str) -> None:
    assert top(query, limit=1) == [expected]


@pytest.mark.xfail(
    strict=True,
    reason="QA-T3-S1: no alias/exonym table, so common Cyrillic spellings whose Latin form is "
    "not a transliteration (Аякс/Ajax, Бавария/Bayern) return nothing",
)
@pytest.mark.parametrize(("query", "expected"), [("Аякс", "Ajax"), ("Бавария", "Bayern München")])
def test_cyrillic_exonyms_find_the_latin_name(query: str, expected: str) -> None:
    candidates = [*CANDIDATES, Candidate(99, "Ajax")]
    assert [hit.name for hit in rank(query, candidates, limit=1)] == [expected]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("arsenal", "Арсенал"),
        ("liverpool", "Ливерпул"),
        ("real madrid", "Реал Мадрид"),
        ("MANCHESTER CITY", "Манчестер Сити"),
    ],
)
def test_latin_query_finds_a_cyrillic_name(query: str, expected: str) -> None:
    candidates = [Candidate(1, expected), Candidate(2, "Барселона"), Candidate(3, "Челси")]
    assert [hit.id for hit in rank(query, candidates, limit=1)] == [1]


@pytest.mark.parametrize(
    "variant",
    ["bayern münchen", "BAYERN MÜNCHEN", "Bayern Munchen", "bayern munchen", "BAYERN  MUNCHEN "],
)
def test_case_and_diacritics_do_not_matter(variant: str) -> None:
    assert top(variant, limit=1) == ["Bayern München"]
    assert search_key(variant) == "bayern munchen"


@pytest.mark.parametrize(
    "variant", ["Nott'm Forest", "nottm forest", "NOTT’M FOREST", "Nottʻm forest"]
)
def test_apostrophe_variants_are_equivalent(variant: str) -> None:
    assert top(variant, limit=1) == ["Nott'm Forest"]


def test_prefix_and_typo_queries() -> None:
    assert top("barc", limit=1) == ["Barcelona"]
    assert top("bar", limit=1) == ["Barcelona"]
    assert top("arsenl", limit=1) == ["Arsenal"]
    assert {hit.name for hit in rank("manch", CANDIDATES, limit=2)} == {
        "Manchester City",
        "Manchester United",
    }


@pytest.mark.parametrize("query", ["", " ", "   ", "'", "---", "!!!", "́", "1234"])
def test_degenerate_queries_return_nothing_and_do_not_crash(query: str) -> None:
    assert rank(query, CANDIDATES) == []


@given(query=st.text(max_size=60), names=st.lists(st.text(max_size=30), max_size=12))
@settings(max_examples=200, deadline=None)
def test_search_never_crashes_and_stays_bounded(query: str, names: list[str]) -> None:
    candidates = [Candidate(index, name) for index, name in enumerate(names)]
    hits = rank(query, candidates, limit=5)
    assert len(hits) <= 5
    assert all(60 <= hit.score <= 100 for hit in hits)
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)
    assert isinstance(to_latin(query), str)
    assert isinstance(to_cyrillic(query), str)


@given(name=st.sampled_from(NAMES), shout=st.booleans())
def test_case_never_changes_the_result(name: str, shout: bool) -> None:
    variant = name.upper() if shout else name.lower()
    assert top(variant) == top(name)


async def test_database_search_matches_across_alphabets(db_session: AsyncSession) -> None:
    fixtures = [
        make_fixture("Arsenal", "Chelsea", status=FixtureStatus.SCHEDULED),
        make_fixture("Манчестер Сити", "Köln", kickoff=KICKOFF, status=FixtureStatus.SCHEDULED),
    ]
    await upsert_live(db_session, fixtures=fixtures)
    cyrillic = await search_teams(db_session, "Арсенал")
    assert [hit.name for hit in cyrillic][:1] == ["Arsenal"]
    latin = await search_teams(db_session, "manchester siti")
    assert [hit.name for hit in latin][:1] == ["Манчестер Сити"]
    umlaut = await search_teams(db_session, "KOLN")
    assert [hit.name for hit in umlaut][:1] == ["Köln"]
    assert await search_teams(db_session, "") == []
    assert await search_teams(db_session, "Арсенал", sport=Sport.FOOTBALL)
