"""🧠 Ekspert tahlili wording: the requested Telegram layout, risk bands and fact reasons."""

from dataclasses import replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.bot import texts
from app.bot.expert_ui import fact_reason, form_text, format_expert_report
from app.bot.picks_ui import pick_label
from app.domain import Market, Selection
from app.services._fake_matches import SAMPLE_FACTS
from app.services.interfaces import (
    ExpertMatch,
    ExpertPick,
    ExpertReport,
    MatchItem,
    MatchState,
    Meeting,
    OutcomeChance,
    TeamChance,
    TeamPick,
    TopPick,
)
from tests.e2e.bot_client import telegram_html_problems

TASHKENT = ZoneInfo("Asia/Tashkent")
DAY = date(2026, 10, 31)
MATCH = MatchItem(
    match_id=7,
    kickoff=datetime(2026, 10, 31, 16, 30, tzinfo=UTC),
    competition="Premier League",
    competition_code="PL",
    home="Arsenal <FC>",
    away="Chelsea FC",
    state=MatchState.SCHEDULED,
)


def outcome(
    market: Market, selection: Selection, probability: float, push: float = 0.0
) -> ExpertPick:
    chance = OutcomeChance(market=market, selection=selection, probability=probability, push=push)
    return ExpertPick(pick=TopPick(match=MATCH, outcome=chance))


def report(*picks: ExpertPick, facts: bool = True) -> ExpertReport:
    expert = ExpertMatch(match=MATCH, picks=picks, facts=SAMPLE_FACTS if facts else None)
    return ExpertReport(matches=(expert,))


def test_the_requested_layout() -> None:
    pick = replace(
        outcome(Market.totals(2.5), Selection.OVER, 0.68), reason="Ikkala jamoa ko'p gol uradi."
    )
    text = format_expert_report(report(pick), day_from=DAY, day_to=DAY, zone=TASHKENT)
    assert "⚽ <b>Arsenal &lt;FC&gt; — Chelsea FC</b>" in text
    assert "🏆 Liga: Premier League | ⏰ Vaqt: 31.10.2026 21:30" in text
    assert "🎯 <b>Tavsiya:</b> Total 2.5 dan ko'p" in text
    assert "📊 <b>O'tish ehtimoli:</b> 68% (🟡 O'rtacha)" in text
    assert "📝 <b>Qisqa sabab:</b> Ikkala jamoa ko'p gol uradi." in text
    assert texts.EXPERT_SOURCE_RULE in text  # no match was chosen by the AI
    assert telegram_html_problems(text) == []


@pytest.mark.parametrize(
    ("probability", "band"),
    [
        (0.71, "🟢 Ishonchli"),
        (0.55, "🟡 O'rtacha"),
        (0.41, "🔴 Tavakkal"),
        (0.2, "⚪ Juda tavakkal"),
    ],
)
def test_risk_bands_are_shown(probability: float, band: str) -> None:
    pick = outcome(Market.match_result(), Selection.HOME, probability)
    assert band in format_expert_report(report(pick), day_from=DAY, day_to=DAY, zone=TASHKENT)


def test_handicap_shows_the_refund_and_a_signed_line() -> None:
    pick = outcome(Market.asian_handicap(-1.0), Selection.AWAY, 0.8, push=0.2)
    text = format_expert_report(report(pick), day_from=DAY, day_to=DAY, zone=TASHKENT)
    assert "Fora (+1) — Chelsea FC" in text
    assert "80% (🟢 Ishonchli) · qaytish 20%" in text
    level = outcome(Market.asian_handicap(0.0), Selection.HOME, 0.5)
    assert pick_label(level.pick) == "Fora (0) — Arsenal &lt;FC&gt;"


def test_reasons_are_worded_from_the_facts_when_the_ai_gave_none() -> None:
    home_win = outcome(Market.double_chance(), Selection.HOME_OR_DRAW, 0.8)
    reason = fact_reason(home_win, MATCH, SAMPLE_FACTS)
    assert reason.startswith("Arsenal &lt;FC&gt; formasi GGDGM, uyda so'nggi 5 o'yinda 4 g'alaba")
    assert "Chelsea FC formasi MDGMM" in reason
    assert "Jadvalda 2- va 14-o'rinlar." in reason
    away_win = outcome(Market.match_result(), Selection.AWAY, 0.5)
    assert fact_reason(away_win, MATCH, SAMPLE_FACTS).startswith(
        "Chelsea FC formasi MDGMM, safarda"
    )

    goals = fact_reason(outcome(Market.totals(2.5), Selection.UNDER, 0.6), MATCH, SAMPLE_FACTS)
    assert "o'rtacha 2.1 gol uradi va 0.9 o'tkazadi" in goals
    team = ExpertPick(
        pick=TopPick(match=MATCH, team=TeamChance(pick=TeamPick.HOME_OVER_1_5, probability=0.6))
    )
    assert fact_reason(team, MATCH, SAMPLE_FACTS) == fact_reason(
        outcome(Market.totals(1.5), Selection.OVER, 0.6), MATCH, SAMPLE_FACTS
    )
    btts = fact_reason(outcome(Market.btts(), Selection.YES, 0.6), MATCH, SAMPLE_FACTS)
    assert "Ikkala jamoa gol urgan o'yinlar: Arsenal &lt;FC&gt; 50%" in btts

    with_h2h = replace(
        SAMPLE_FACTS,
        meetings=(Meeting(day=date(2026, 3, 1), home="A&B", away="C", home_goals=2, away_goals=0),),
    )
    assert "O'zaro so'nggi o'yin: A&amp;B 2-0 C (01.03.2026)." in fact_reason(
        home_win, MATCH, with_h2h
    )
    assert fact_reason(home_win, MATCH, None) == texts.EXPERT_REASON_MODEL


def test_table_is_left_out_without_positions() -> None:
    facts = replace(SAMPLE_FACTS, away=replace(SAMPLE_FACTS.away, position=None))
    pick = outcome(Market.match_result(), Selection.HOME, 0.6)
    assert "Jadvalda" not in fact_reason(pick, MATCH, facts)


def test_empty_report_and_the_ai_note() -> None:
    empty = format_expert_report(
        ExpertReport(without_forecast=3), day_from=DAY, day_to=date(2026, 11, 1), zone=TASHKENT
    )
    assert "31.10.2026 — 01.11.2026" in empty
    assert texts.EXPERT_EMPTY in empty
    assert texts.EXPERT_WITHOUT_FORECAST.format(count=3) in empty
    pick = replace(outcome(Market.btts(), Selection.NO, 0.6), reason="Sabab.")
    ai = ExpertReport(matches=(ExpertMatch(match=MATCH, picks=(pick,), by_ai=True),))
    assert texts.EXPERT_SOURCE_AI in format_expert_report(
        ai, day_from=DAY, day_to=DAY, zone=TASHKENT
    )


def test_form_letters() -> None:
    assert form_text("WDL") == "GDM"
    assert form_text("") == "—"
