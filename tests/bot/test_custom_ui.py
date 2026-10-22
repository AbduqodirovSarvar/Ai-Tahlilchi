"""Pure parts of 🔍 Maxsus tahlil: the fixture-text parser, the draft and the result block."""

from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.bot import texts
from app.bot.custom_ui import (
    MAX_PICK_LINES,
    CustomDraft,
    CustomOption,
    by_fixture,
    format_custom_fixture,
    render_custom,
)
from app.bot.draft import StaleDraftError
from app.bot.parsing import parse_fixture_text
from app.domain import Market, Selection
from tests.bot.samples import make_signal
from tests.e2e.bot_client import buttons_of, telegram_html_problems

TASHKENT = ZoneInfo("Asia/Tashkent")


# --------------------------------------------------------------------------- parser
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Arsenal - Chelsea", ("Arsenal", "Chelsea")),
        ("Arsenal – Chelsea", ("Arsenal", "Chelsea")),
        ("  Real   Madrid  —  Barcelona ", ("Real Madrid", "Barcelona")),
        ("Arsenal vs Chelsea", ("Arsenal", "Chelsea")),
        ("Arsenal VS. Chelsea", ("Arsenal", "Chelsea")),
        ("Arsenal v Chelsea", ("Arsenal", "Chelsea")),
        ("Saint-Etienne - Lyon", ("Saint-Etienne", "Lyon")),
        ("Арсенал - Челси", ("Арсенал", "Челси")),
    ],
)
def test_fixture_text_splits_into_two_names(text: str, expected: tuple[str, str]) -> None:
    assert parse_fixture_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Arsenal",
        "Arsenal Chelsea",
        "Saint-Etienne",
        "25.10 - 31.10",
        "ertaga 60% Arsenal - Chelsea",
        "Arsenal, Inter - Chelsea",
        "Arsenal - ",
        "- Chelsea",
        "A - Chelsea",
        "Arsenal - Chelsea - Inter",
        "x" * 400 + " - y",
    ],
)
def test_other_text_is_not_a_fixture(text: str) -> None:
    assert parse_fixture_text(text) is None


@given(st.text(max_size=120))
def test_parser_never_fails_and_returns_clean_names(text: str) -> None:
    result = parse_fixture_text(text)

    if result is not None:
        home, away = result
        assert home == home.strip() and away == away.strip()
        assert home and away
        assert not any(char.isdigit() or char in "%," for char in home + away)


# --------------------------------------------------------------------------- draft
def make_draft(**overrides: object) -> CustomDraft:
    options = (
        CustomOption("f1", "22.10 20:00 Arsenal — Chelsea", "football", "Arsenal — Chelsea"),
        CustomOption("f2", "23.10 21:00 Inter — Lazio", "football", "Inter — Lazio"),
        CustomOption("f3", "23.10 17:00 SKA — CSKA", "hockey", "SKA — CSKA"),
    )
    base = CustomDraft(message_id=7, kind="fixtures", title="Arsenal", options=options)
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_draft_round_trips_through_json_safe_data() -> None:
    draft = make_draft(selected=("f1", "f3"), truncated=4)

    restored = CustomDraft.from_data(draft.to_data())

    assert restored == draft
    assert restored.sports == {"football", "hockey"}


@pytest.mark.parametrize(
    "data",
    [None, "x", {}, {"kind": "fixtures"}, {**make_draft().to_data(), "kind": "weird"}],
)
def test_corrupt_data_is_stale(data: object) -> None:
    with pytest.raises(StaleDraftError):
        CustomDraft.from_data(data)


def test_toggling_adds_removes_and_rejects_bad_indexes() -> None:
    draft = make_draft()

    on = draft.toggled(1)
    assert on.selected == ("f2",)
    assert on.toggled(1).selected == ()
    for index in (-1, 3):
        with pytest.raises(StaleDraftError):
            draft.toggled(index)


def test_screens_have_the_expected_buttons() -> None:
    prompt_text, prompt = render_custom(CustomDraft())
    assert prompt_text == texts.CUSTOM_PROMPT
    assert [b.text for b in buttons_of(prompt)] == [
        texts.BTN_CUSTOM_TODAY,
        texts.BTN_CUSTOM_TOMORROW,
        texts.BTN_CANCEL,
    ]

    teams = make_draft(kind="teams", title="Re<al>")
    teams_text, teams_markup = render_custom(teams)
    assert "Re&lt;al&gt;" in teams_text
    assert len(buttons_of(teams_markup)) == 3 + 2

    chosen = make_draft(selected=("f2",))
    text, markup = render_custom(chosen)
    labels = [b.text for b in buttons_of(markup)]
    assert "Tanlangan: 1" in text
    assert labels[1].startswith("✅") and labels[0].startswith("▫️")
    assert texts.BTN_CUSTOM_RUN.format(count=1) in labels
    assert texts.BTN_CUSTOM_RUN.format(count=0) not in [
        b.text for b in buttons_of(render_custom(make_draft())[1])
    ]


def test_every_custom_button_fits_callback_data() -> None:
    draft = make_draft(selected=("f1",))
    for screen in (CustomDraft(), make_draft(kind="teams"), draft):
        for button in buttons_of(render_custom(screen)[1]):
            assert len((button.callback_data or "").encode()) <= 64


# --------------------------------------------------------------------------- result block
def test_fixture_block_lists_every_selection_and_flags_value() -> None:
    value = make_signal(1)
    draw = make_signal(
        2, selection=Selection.DRAW, probability=0.24, odds=3.2, ev=-0.23, recommended_at=None
    )
    over = make_signal(
        3,
        market=Market.totals(2.5),
        selection=Selection.OVER,
        probability=0.5,
        odds=2.05,
        ev=0.025,
        recommended_at=None,
        no_value=True,
    )

    text = format_custom_fixture([draw, over, value], TASHKENT)

    lines = text.splitlines()
    assert lines[0].startswith("⚽ <b>Arsenal — Chelsea</b>")
    assert lines[1].startswith("✅ Arsenal g'alabasi (1): 61% · @1.85 · EV +12.9%")
    assert lines[2].startswith("▫️ Total 2.5 dan ko'p: 50%")
    assert lines[3].startswith("❗ Durang (X): 24% · @3.20 · EV -23.0%")
    assert texts.CUSTOM_LEGEND in text
    assert telegram_html_problems(text) == []


def test_fixture_without_value_says_so() -> None:
    signal = make_signal(1, recommended_at=None, no_value=True, ev=0.01)

    text = format_custom_fixture([signal], TASHKENT)

    assert texts.CUSTOM_NO_VALUE_NOTE in text
    assert texts.CUSTOM_LEGEND not in text


def test_a_long_selection_list_is_cut() -> None:
    signals = [
        make_signal(i, market=Market.totals(0.5 + i * 0.25), selection=Selection.OVER)
        for i in range(1, MAX_PICK_LINES + 6)
    ]

    text = format_custom_fixture(signals, TASHKENT)

    assert texts.CUSTOM_MORE_PICKS.format(count=5) in text
    assert len(text) < 4096


def test_hostile_names_are_escaped_in_the_block() -> None:
    signal = make_signal(1, home_team="<i>A&B</i>", league="L<>")

    text = format_custom_fixture([signal], TASHKENT)

    assert "&lt;i&gt;A&amp;B&lt;/i&gt;" in text
    assert telegram_html_problems(text) == []


def test_signals_are_grouped_by_fixture_in_kickoff_order() -> None:
    late = make_signal(1, fixture_id="late", kickoff=make_signal().kickoff.replace(hour=20))
    early_a = make_signal(2, fixture_id="early")
    early_b = make_signal(3, fixture_id="early", selection=Selection.AWAY)

    groups = by_fixture([late, early_a, early_b])

    assert [[s.signal_id for s in g] for g in groups] == [[2, 3], [1]]
