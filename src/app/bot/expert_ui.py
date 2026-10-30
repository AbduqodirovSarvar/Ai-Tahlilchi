"""🧠 Ekspert tahlili: 1-2 picks for every match, with probability, risk band and a short reason.

The service chooses (:meth:`app.services.interfaces.MatchesService.expert_picks`); this module
only words the result. The AI's reason is shown as given (escaped); without one, the reason is
worded here from the history facts, so every pick says why.
"""

from datetime import date
from html import escape
from zoneinfo import ZoneInfo

from app.bot import texts
from app.bot.formatters import kickoff_stamp, percent
from app.bot.picks_ui import pick_label, window_text
from app.domain import MarketType, Selection
from app.services.interfaces import ExpertMatch, ExpertPick, ExpertReport, MatchFacts, MatchItem

_HOME_SIDE = frozenset(
    {Selection.HOME, Selection.HOME_OR_DRAW, Selection.DRAW, Selection.HOME_OR_AWAY}
)
_GOAL_MARKETS = frozenset({MarketType.TOTALS})


def format_expert_report(
    report: ExpertReport, *, day_from: date, day_to: date, zone: ZoneInfo
) -> str:
    """The whole report; the caller splits it into Telegram-sized messages."""
    by_ai = any(match.by_ai for match in report.matches)
    lines = [
        texts.EXPERT_TITLE.format(window=window_text(day_from, day_to), count=len(report.matches)),
        texts.EXPERT_NOTE.format(
            source=texts.EXPERT_SOURCE_AI if by_ai else texts.EXPERT_SOURCE_RULE
        ),
    ]
    if not report.matches:
        lines.extend(["", texts.EXPERT_EMPTY])
    for match in report.matches:
        lines.extend(["", *expert_match_lines(match, zone)])
    if report.without_forecast:
        lines.extend(["", texts.EXPERT_WITHOUT_FORECAST.format(count=report.without_forecast)])
    return "\n".join(lines)


def expert_match_lines(expert: ExpertMatch, zone: ZoneInfo) -> list[str]:
    """One match block: teams, league and time, then each pick with its reason."""
    match = expert.match
    lines = [
        texts.EXPERT_MATCH.format(home=_name(match.home), away=_name(match.away)),
        texts.EXPERT_META.format(
            league=_text(match.competition) or "—", kickoff=kickoff_stamp(match.kickoff, zone)
        ),
    ]
    for pick in expert.picks:
        lines.extend(_pick_lines(pick, match, expert.facts))
    return lines


def _pick_lines(pick: ExpertPick, match: MatchItem, facts: MatchFacts | None) -> list[str]:
    outcome = pick.pick.outcome
    push = (
        texts.EXPERT_PUSH.format(push=percent(outcome.push))
        if outcome is not None and outcome.push > 0
        else ""
    )
    reason = _text(pick.reason) if pick.reason else fact_reason(pick, match, facts)
    return [
        texts.EXPERT_PICK.format(label=pick_label(pick.pick)),
        texts.EXPERT_PROBABILITY.format(
            probability=percent(pick.probability),
            risk=texts.PICK_RISK_LABELS[pick.risk.value],
            push=push,
        ),
        texts.EXPERT_REASON.format(reason=reason),
    ]


def fact_reason(pick: ExpertPick, match: MatchItem, facts: MatchFacts | None) -> str:
    """A reason worded from the history facts (used when the AI gave none)."""
    if facts is None:
        return texts.EXPERT_REASON_MODEL
    outcome = pick.pick.outcome
    if outcome is None or outcome.market.type in _GOAL_MARKETS:
        return _goals_reason(match, facts) + _h2h(facts)
    if outcome.market.type is MarketType.BTTS:
        return _btts_reason(match, facts) + _h2h(facts)
    return _result_reason(match, facts, home_side=outcome.selection in _HOME_SIDE) + _h2h(facts)


def _result_reason(match: MatchItem, facts: MatchFacts, *, home_side: bool) -> str:
    own, other = (facts.home, facts.away) if home_side else (facts.away, facts.home)
    team, other_team = (match.home, match.away) if home_side else (match.away, match.home)
    venue = own.venue
    text = texts.EXPERT_REASON_RESULT.format(
        team=_name(team),
        form=form_text(own.form),
        venue=texts.EXPERT_VENUE[home_side],
        played=venue.played,
        wins=venue.wins,
        draws=venue.draws,
        losses=venue.losses,
        other=_name(other_team),
        other_form=form_text(other.form),
    )
    if facts.home.position is not None and facts.away.position is not None:
        text += texts.EXPERT_REASON_TABLE.format(
            home_position=facts.home.position, away_position=facts.away.position
        )
    return text


def _goals_reason(match: MatchItem, facts: MatchFacts) -> str:
    return texts.EXPERT_REASON_GOALS.format(
        home=_name(match.home),
        away=_name(match.away),
        home_for=_avg(facts.home.goals_for_avg),
        home_against=_avg(facts.home.goals_against_avg),
        away_for=_avg(facts.away.goals_for_avg),
        away_against=_avg(facts.away.goals_against_avg),
        home_over=percent(facts.home.over_2_5_share),
        away_over=percent(facts.away.over_2_5_share),
    )


def _btts_reason(match: MatchItem, facts: MatchFacts) -> str:
    return texts.EXPERT_REASON_BTTS.format(
        home=_name(match.home),
        away=_name(match.away),
        home_btts=percent(facts.home.btts_share),
        away_btts=percent(facts.away.btts_share),
        home_for=_avg(facts.home.goals_for_avg),
        away_for=_avg(facts.away.goals_for_avg),
    )


def _h2h(facts: MatchFacts) -> str:
    if not facts.meetings:
        return ""
    last = facts.meetings[0]
    return texts.EXPERT_REASON_H2H.format(
        home=_text(last.home),
        away=_text(last.away),
        home_goals=last.home_goals,
        away_goals=last.away_goals,
        day=last.day.strftime("%d.%m.%Y"),
    )


def form_text(form: str) -> str:
    """``WDL`` in Uzbek letters (``GDM``); ``—`` without matches."""
    return "".join(texts.FORM_LETTERS.get(letter, letter) for letter in form) or "—"


def _avg(value: float) -> str:
    return f"{value:.1f}"


def _name(name: str | None) -> str:
    return _text(name) if name else texts.MATCHES_TBD


def _text(value: str) -> str:
    """HTML-safe text outside attributes: apostrophes (``ko'p``) stay readable."""
    return escape(value, quote=False)
