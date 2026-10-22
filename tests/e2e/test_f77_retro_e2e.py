"""Retro analysis UX through the real dispatcher over the fake services (F7.7).

Decision #12: a window is at most 31 days long and starts at most 90 days before today.
The fixed test clock is Wednesday 21.10.2026.
"""

from datetime import date

import pytest

from app.bot import texts
from app.bot.parsing import InputError, parse_quick_query
from app.services.interfaces import DateMode
from tests.e2e.bot_client import Client, shown

TODAY = date(2026, 10, 21)


def quick(text: str) -> object:
    return parse_quick_query(text, TODAY, probability_bounds=(0.3, 0.95), ev_bounds=(0.0, 0.2))


# --------------------------------------------------------------------------- parser
@pytest.mark.parametrize(
    ("text", "first", "last"),
    [
        ("kecha", date(2026, 10, 20), date(2026, 10, 20)),
        ("o'tgan hafta", date(2026, 10, 14), date(2026, 10, 20)),
        ("otgan hafta futbol", date(2026, 10, 14), date(2026, 10, 20)),
        ("KECHA 60%", date(2026, 10, 20), date(2026, 10, 20)),
    ],
)
def test_retro_keywords_resolve_to_past_ranges(text: str, first: date, last: date) -> None:
    parsed = quick(text)

    spec = parsed.dates  # type: ignore[attr-defined]
    assert spec.mode is DateMode.RANGE
    assert (spec.date_from, spec.date_to) == (first, last)
    assert spec.resolve(TODAY).is_retro(TODAY)


def test_two_date_keywords_are_still_a_duplicate() -> None:
    with pytest.raises(InputError):
        quick("kecha bugun")


# --------------------------------------------------------------------------- wizard buttons
async def test_date_step_offers_retro_shortcuts(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)

    assert admin.has_button(texts.BTN_YESTERDAY)
    assert admin.has_button(texts.BTN_LAST_WEEK)


async def run_retro(admin: Client, button: str) -> list[str]:
    await admin.send(texts.MENU_ANALYSIS)
    await admin.press(button)
    await admin.press("Futbol")
    await admin.press("50%")
    await admin.press("5%")
    await admin.press(texts.BTN_SCOPE_ALL)
    await admin.press(texts.BTN_ALL)
    await admin.press(texts.BTN_DONE)
    await admin.press(texts.SORT_LABELS[next(iter(texts.SORT_LABELS))])
    confirm = admin.last_text
    assert "🕰" in confirm  # the confirmation says it is a look-back
    methods = await admin.press(texts.BTN_START)
    return shown(methods)


async def test_yesterday_wizard_run_shows_results_and_a_tally(admin: Client) -> None:
    blocks = await run_retro(admin, texts.BTN_YESTERDAY)

    summary = next(text for text in blocks if "Natijalar" in text or "Value topilmadi" in text)
    assert texts.RESULTS_RETRO in summary


async def test_last_week_wizard_confirmation_names_the_range(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)

    methods = await admin.press(texts.BTN_LAST_WEEK)

    assert "Sport turi" in shown(methods)[0]


# --------------------------------------------------------------------------- /tahlil
async def test_tahlil_kecha_confirms_the_look_back(admin: Client) -> None:
    methods = await admin.send("/tahlil kecha 30% ev0%")

    text = shown(methods)[-1]
    assert "20.10" in text
    assert texts.WIZARD_CONFIRM_RETRO in text
    assert admin.has_button(texts.BTN_START)


async def test_retro_cards_show_the_result_and_have_no_bet_or_recalc(admin: Client) -> None:
    await admin.send("/tahlil o'tgan hafta 30% ev0%")
    methods = await admin.press(texts.BTN_START)

    blocks = shown(methods)
    assert any(texts.RESULTS_RETRO in text for text in blocks)
    cards = [text for text in blocks if "Retro:" in text]
    assert cards
    assert any("Natija:" in text for text in cards)
    assert not admin.has_button(texts.BTN_BET)
    assert not admin.has_button(texts.BTN_RECALC)
    assert not admin.has_button(texts.BTN_RECALC_ALL)


async def test_retro_tally_matches_the_card_results(admin: Client) -> None:
    await admin.send("/tahlil o'tgan hafta 30% ev0% 5ta")
    methods = await admin.press(texts.BTN_START)

    blocks = shown(methods)
    summary = next((t for t in blocks if texts.RESULTS_RETRO in t), "")
    cards = [t for t in blocks if "Retro:" in t]
    won = sum("Natija: ✅ O'tdi" in t or "Yarim o'tdi" in t for t in cards)
    lost = sum("Natija: ❌ O'tmadi" in t or "Yarim o'tmadi" in t for t in cards)
    if won + lost:
        assert f"✅ {won} · ❌ {lost}" in summary
    else:
        assert "🧮" not in summary


async def test_retro_signals_stay_out_of_the_report(admin: Client) -> None:
    await admin.send("/tahlil o'tgan hafta 30% ev0%")
    await admin.press(texts.BTN_START)

    (text,) = shown(await admin.send(texts.MENU_REPORT))

    assert texts.REPORT_EMPTY in text


# --------------------------------------------------------------------------- limits
async def test_a_range_older_than_90_days_is_refused_with_the_rule(admin: Client) -> None:
    methods = await admin.send("/tahlil 01.07-05.07")

    assert "juda eski" in shown(methods)[0]
    assert "90" in shown(methods)[0]


async def test_a_retro_window_longer_than_31_days_is_refused(admin: Client) -> None:
    methods = await admin.send("/tahlil 01.09-20.10")

    assert "juda uzun" in shown(methods)[0]
