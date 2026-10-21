"""Team search: transliteration, normalisation, ranking and the database lookup."""

# Cyrillic test strings contain look-alike letters on purpose.
# ruff: noqa: RUF001

from datetime import timedelta

from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import upsert_live
from app.db.search import Candidate, rank, search_key, search_teams, to_cyrillic, to_latin
from app.domain import ExternalRef, Sport
from tests.domain.factories import KICKOFF, make_fixture, make_league

TEAMS = [
    Candidate(1, "Barcelona"),
    Candidate(2, "Bayern Munich"),
    Candidate(3, "Man United"),
    Candidate(4, "Man City"),
    Candidate(5, "Paxtakor"),
    Candidate(6, "Nott'm Forest"),
    Candidate(7, "Real Madrid"),
    Candidate(8, "Ath Madrid"),
    Candidate(9, "Bayer Leverkusen"),
]


def names(query: str, limit: int = 3) -> list[str]:
    return [hit.name for hit in rank(query, TEAMS, limit=limit)]


def test_cyrillic_to_latin() -> None:
    assert to_latin("Пахтакор") == "paxtakor"
    assert to_latin("Ўзбекистон") == "o'zbekiston"
    assert to_latin("Ғалаба Шўрчи") == "g'alaba sho'rchi"
    assert to_latin("Манчестер Юнайтед") == "manchester yunayted"


def test_latin_to_cyrillic() -> None:
    assert to_cyrillic("Paxtakor") == "пахтакор"
    assert to_cyrillic("Oʻzbekiston") == "ўзбекистон"
    assert to_cyrillic("g'alaba shoʼrchi") == "ғалаба шўрчи"
    assert to_cyrillic("Real 2") == "реал 2"


@given(st.text(alphabet="abdefghijklmnopqrstuvxyz' ", max_size=30))
def test_uzbek_latin_round_trips_through_cyrillic(text: str) -> None:
    assert to_latin(to_cyrillic(text)) == text


@given(st.text(alphabet="абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ -", max_size=30))
def test_cyrillic_search_keys_are_plain_latin(text: str) -> None:
    key = search_key(text)
    assert key.isascii()
    assert key == search_key(text.upper())


def test_search_key_is_case_accent_and_apostrophe_insensitive() -> None:
    assert search_key("NOTT’M Forest") == search_key("nottm forest") == "nottm forest"
    assert search_key("Bayern München") == "bayern munchen"
    assert search_key("  ") == ""


def test_exact_and_prefix_matches_rank_first() -> None:
    assert names("barcelona")[0] == "Barcelona"
    assert names("bar")[0] == "Barcelona"
    assert names("madrid", limit=2) == ["Ath Madrid", "Real Madrid"]  # word prefix, shorter first
    assert names("leverk")[0] == "Bayer Leverkusen"


def test_typos_and_cyrillic_queries() -> None:
    assert names("barselona")[0] == "Barcelona"
    assert names("Пахтакор")[0] == "Paxtakor"
    assert names("Барселона")[0] == "Barcelona"
    assert names("man unitd")[0] == "Man United"


def test_no_match_and_empty_queries() -> None:
    assert names("zzzzqqq") == []
    assert rank("", TEAMS) == []
    assert rank("!!!", TEAMS) == []
    assert rank("bar", TEAMS, limit=0) == []
    assert rank("bar", []) == []


@given(st.text(max_size=40))
def test_rank_never_crashes_and_is_bounded(query: str) -> None:
    hits = rank(query, TEAMS, limit=5)
    assert len(hits) <= 5
    assert all(0 <= hit.score <= 100 for hit in hits)
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)


@given(st.sampled_from([c.name for c in TEAMS]))
def test_case_and_diacritics_do_not_change_results(name: str) -> None:
    assert names(name.upper()) == names(name.lower())


async def test_search_teams_in_database(db_session: AsyncSession) -> None:
    epl = make_league()
    bundesliga = make_league("D1", "Bundesliga")
    await upsert_live(
        db_session,
        fixtures=[
            make_fixture("Man United", "Fulham", league=epl),
            make_fixture("Man City", "Fulham", league=epl, kickoff=KICKOFF - timedelta(days=7)),
            make_fixture("Man City", "Bayern Munich", league=bundesliga),
        ],
    )
    hits = await search_teams(db_session, "man", sport=Sport.FOOTBALL, limit=5)
    assert [hit.name for hit in hits][:2] == ["Man City", "Man United"]
    by_name = {hit.name: hit for hit in hits}
    assert by_name["Man City"].league == "Bundesliga"  # most recent fixture
    assert by_name["Man United"].ref == ExternalRef("test-source", "Man United")
    assert await search_teams(db_session, "man", sport=Sport.HOCKEY) == []
    assert await search_teams(db_session, "qqqzzz") == []
