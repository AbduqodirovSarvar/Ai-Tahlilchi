"""QA: every settings field is editable (F6.3) and results are presented per §6.2 (F6.6)."""

from collections.abc import Callable
from dataclasses import replace
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod

from app.bot import texts
from app.bot.handlers.analysis_run import PROGRESS_MIN_INTERVAL, ProgressReporter
from app.bot.messaging import install_flood_control
from app.domain import MarketType, Sport
from app.services.interfaces import (
    AnalysisProgress,
    AnalysisQuery,
    AnalysisResult,
    AnalysisStage,
    RiskProfile,
    Services,
    SignalView,
    UserSettingsView,
    WatchKind,
    WatchTarget,
)
from tests.bot.fakes import ADMIN_ID, FIXED_NOW, RecordingSession
from tests.e2e.bot_client import Client, answers, make_dispatcher, sent, shown
from tests.e2e.flows import to_confirm

TASHKENT = ZoneInfo("Asia/Tashkent")


# --------------------------------------------------------------------------- settings
Check = Callable[[UserSettingsView], bool]
SETTINGS_CASES: list[Any] = [
    ("sports", [("opt", "🏒 Xokkey 🧪")], lambda s: Sport.HOCKEY in s.sports),
    ("leagues", [("opt", "Premier League")], lambda s: s.league_ids == {"af:l39"}),
    ("markets", [("opt", "Ikkala jamoa gol uradi")], lambda s: MarketType.BTTS in s.markets),
    ("max_signals", [("opt", "20")], lambda s: s.max_signals == 20),
    ("max_signals", [("input", "25")], lambda s: s.max_signals == 25),
    ("min_probability", [("opt", "60%")], lambda s: s.min_probability == pytest.approx(0.6)),
    ("min_probability", [("input", "62,5")], lambda s: s.min_probability == pytest.approx(0.625)),
    ("min_ev", [("opt", "8%")], lambda s: s.min_ev == pytest.approx(0.08)),
    ("min_ev", [("input", "4.5%")], lambda s: s.min_ev == pytest.approx(0.045)),
    (
        "odds",
        [("opt", f"2.00{texts.RANGE_DASH}5.00")],
        lambda s: (s.odds_min, s.odds_max) == (2.0, 5.0),
    ),
    ("odds", [("input", "1.45-2.80")], lambda s: (s.odds_min, s.odds_max) == (1.45, 2.8)),
    (
        "risk",
        [("opt", texts.RISK_LABELS[RiskProfile.CONSERVATIVE])],
        lambda s: (
            s.risk_profile is RiskProfile.CONSERVATIVE
            and (s.min_probability, s.min_ev, s.odds_min, s.odds_max) == (0.6, 0.03, 1.3, 2.0)
        ),
    ),
    ("send_time", [("opt", "07:00")], lambda s: s.send_time == time(7)),
    ("send_time", [("input", "08:30")], lambda s: s.send_time == time(8, 30)),
    ("prematch", [("opt", "90 daq.")], lambda s: s.prematch_minutes == 90),
    ("prematch", [("opt", texts.OFF)], lambda s: s.prematch_minutes is None),
    ("quiet", [("opt", f"23:00{texts.RANGE_DASH}07:00")], lambda s: s.quiet_hours is not None),
    ("quiet", [("input", "22:30-06:00")], lambda s: str(s.quiet_hours.start) == "22:30:00"),  # type: ignore[union-attr]
    ("quiet", [("off", "")], lambda s: s.quiet_hours is None),
    ("timezone", [("opt", "Europe/Moscow")], lambda s: s.timezone == "Europe/Moscow"),
    ("timezone", [("input", "America/New_York")], lambda s: s.timezone == "America/New_York"),
    ("bank", [("input", "1 000 000")], lambda s: s.bank == Decimal(1_000_000)),
    ("unit", [("input", "5000")], lambda s: s.unit == Decimal(5000)),
    ("daily_loss", [("input", "50 000")], lambda s: s.daily_loss_limit == Decimal(50_000)),
    ("weekly_loss", [("input", "150000")], lambda s: s.weekly_loss_limit == Decimal(150_000)),
]


async def _apply(client: Client, field: str, steps: list[tuple[str, str]]) -> None:
    await client.press_option(texts.SETTINGS_BUTTONS[field])
    for kind, value in steps:
        if kind == "opt":
            await client.press_option(value)
        elif kind == "off":
            await client.press_option(texts.BTN_OFF)
        else:
            await client.press_option(texts.BTN_CUSTOM)
            methods = await client.send(value)
            assert shown(methods)[0].startswith(texts.SETTINGS_SAVED), shown(methods)


def test_all_sixteen_fields_are_covered() -> None:
    assert len(texts.SETTINGS_BUTTONS) == 16
    assert {case[0] for case in SETTINGS_CASES} == set(texts.SETTINGS_BUTTONS)


@pytest.mark.parametrize(("field", "steps", "check"), SETTINGS_CASES)
async def test_every_settings_field_is_editable(
    admin: Client, services: Services, field: str, steps: list[tuple[str, str]], check: Check
) -> None:
    if field == "unit":  # unit needs a bank (unit ≤ bank)
        await services.settings.update_settings(
            admin.user_id,
            _with_bank(await services.settings.get_settings(admin.user_id)),
        )
    await admin.send(texts.MENU_SETTINGS)
    before = await services.settings.get_settings(admin.user_id)

    await _apply(admin, field, steps)

    after = await services.settings.get_settings(admin.user_id)
    assert after != before
    assert check(after)
    assert await admin.state() is None


def _with_bank(settings: UserSettingsView) -> UserSettingsView:
    return replace(settings, bank=Decimal(1_000_000))


async def test_saved_settings_feed_the_next_wizard(admin: Client) -> None:
    await admin.send(texts.MENU_SETTINGS)
    await _apply(admin, "min_probability", [("opt", "60%")])
    await _apply(admin, "min_ev", [("opt", "8%")])

    await admin.send(texts.MENU_ANALYSIS)
    await admin.press(texts.BTN_TODAY)
    await admin.press(texts.SPORT_LABELS[Sport.FOOTBALL])
    await admin.press(texts.BTN_DEFAULT)
    await admin.press(texts.BTN_DEFAULT)
    assert texts.WIZARD_SCOPE.split("\n")[0] in admin.last_text

    methods = await admin.send("/tahlil ertaga")
    assert "≥ 60% · EV ≥ 8%" in shown(methods)[-1]


# --------------------------------------------------------------------------- results
class ResultSpy:
    """Records what ``analyze`` returned."""

    def __init__(self, services: Services) -> None:
        self.results: list[AnalysisResult] = []
        self._real = services.analysis.analyze
        services.analysis.analyze = self  # type: ignore[method-assign]

    async def __call__(self, user_id: int, query: AnalysisQuery, **kwargs: Any) -> AnalysisResult:
        result = await self._real(user_id, query, **kwargs)
        self.results.append(result)
        return result


def _page_through(client: Client, methods: list[TelegramMethod[Any]]) -> Any:
    async def run() -> list[TelegramMethod[Any]]:
        collected = list(methods)
        for _ in range(50):
            if not client.has_button("Yana"):
                break
            collected += await client.press("Yana")
        return collected

    return run()


def _stream(methods: list[TelegramMethod[Any]]) -> list[str]:
    """Header and card messages in the order the user receives them."""
    return [
        method.text
        for method in methods
        if isinstance(method, SendMessage)
        and (method.text.startswith(("📅", "⭐")) or method.text[:1] in "⚽🏒")
    ]


def _signal_for(card: str, signals: list[SignalView], zone: ZoneInfo) -> SignalView:
    for signal in signals:
        local = signal.kickoff.astimezone(zone).strftime("%H:%M")
        if signal.fixture_label in card and f"🕒 {local}" in card:
            return signal
    msg = f"no signal for card {card[:60]!r}"
    raise AssertionError(msg)


async def test_day_groups_follow_the_user_time_zone(admin: Client, services: Services) -> None:
    await admin.send(texts.MENU_SETTINGS)
    await _apply(admin, "timezone", [("input", "Pacific/Auckland")])
    zone = ZoneInfo("Pacific/Auckland")
    spy = ResultSpy(services)
    await admin.send("/tahlil 7kun hammasi 30% ev0% 100ta")

    methods = await _page_through(admin, await admin.press(texts.BTN_START))

    signals = list(spy.results[0].signals)
    assert any(
        s.kickoff.astimezone(zone).date() != s.kickoff.astimezone(TASHKENT).date() for s in signals
    ), "fixture data must make the time zone matter"
    current_day: date | None = None
    seen_days: list[date] = []
    cards = 0
    for text in _stream(methods):
        if text.startswith("📅"):
            day_text = text.split(", ", 1)[1][:5]
            current_day = next(
                s.kickoff.astimezone(zone).date()
                for s in signals
                if s.kickoff.astimezone(zone).strftime("%d.%m") == day_text
            )
            weekday = texts.WEEKDAYS[current_day.weekday()]
            assert text.startswith(f"📅 <b>{weekday}, {day_text}</b>")
            if "(davomi)" not in text:
                seen_days.append(current_day)
                count = sum(1 for s in signals if s.kickoff.astimezone(zone).date() == current_day)
                assert f"— {count} ta signal" in text
            continue
        signal = _signal_for(text, signals, zone)
        assert signal.kickoff.astimezone(zone).date() == current_day
        cards += 1
    assert seen_days == sorted(seen_days)
    assert len(seen_days) == len(set(seen_days))
    assert cards == len(signals)


async def test_pagination_shows_every_signal_once(admin: Client, services: Services) -> None:
    spy = ResultSpy(services)
    await admin.send("/tahlil 7kun 30% ev0% 100ta")
    first = await admin.press(texts.BTN_START)
    total = len(spy.results[0].signals)
    assert total > 10

    methods = await _page_through(admin, first)

    cards = [text for text in _stream(methods) if not text.startswith("📅")]
    assert len(cards) == total
    assert len(set(cards)) == total
    texts_shown = shown(methods)
    pages = -(-total // 5)
    for page in range(1, pages):
        assert texts.RESULTS_MORE.format(shown=page * 5, total=total) in texts_shown
    assert texts_shown[-1] == texts.RESULTS_END.format(total=total)
    # Each "➡️ Yana" removes its own button: tapping it again cannot resend a page.
    assert not admin.has_button("Yana")
    assert any("(davomi)" in text for text in texts_shown) or total % 5 == 0


async def test_cards_follow_plan_layout(admin: Client, services: Services) -> None:
    spy = ResultSpy(services)
    await admin.send("/tahlil 7kun 50% ev3% 100ta")
    methods = await _page_through(admin, await admin.press(texts.BTN_START))
    signals = list(spy.results[0].signals)
    preliminary_seen = 0

    for card in [t for t in _stream(methods) if not t.startswith("📅")]:
        signal = _signal_for(card, signals, TASHKENT)
        lines = card.split("\n")
        assert lines[0].startswith(f"⚽ <b>{signal.fixture_label}</b>")
        assert f"🏆 {signal.league}" in lines[0]
        assert lines[1].startswith("🎯 Tanlov: <b>")
        assert f"@ <b>{signal.odds:.2f}</b>" in lines[1]
        assert lines[2] == (
            f"📊 Ehtimollik: {signal.probability * 100:.0f}% "
            f"(adolatli koeff. {1 / signal.probability:.2f})"
        )
        assert lines[3].startswith(f"💹 EV: +{signal.ev * 100:.1f}%   🛡 Ishonch: ")
        assert lines[4] == f"💰 Tavsiya: {signal.stake_units.normalize():f} unit"
        assert (texts.CARD_PRELIMINARY in card) is signal.preliminary
        assert signal.preliminary is (signal.kickoff - FIXED_NOW > timedelta(days=3))
        preliminary_seen += signal.preliminary
    assert preliminary_seen


async def test_watchlist_block_is_outside_count_and_shortfall(
    admin: Client, services: Services
) -> None:
    arsenal = (await services.catalog.search_teams("Arsenal", sport=Sport.FOOTBALL))[0]
    await services.watchlist.add_watch(
        admin.user_id, WatchTarget(kind=WatchKind.TEAM, entity_id=arsenal.team_id)
    )
    spy = ResultSpy(services)
    await admin.send("/tahlil 7kun 50% ev3% 2ta")

    methods = await _page_through(admin, await admin.press(texts.BTN_START))

    result = spy.results[0]
    watched = [s for s in result.signals if s.watchlist]
    regular = [s for s in result.signals if not s.watchlist]
    assert watched
    assert len(regular) <= 2
    summary = next(t for t in shown(methods) if t.startswith("📊 <b>Natijalar</b>"))
    assert f"· {len(regular)} ta signal ·" in summary
    assert texts.RESULTS_WATCHLIST.format(count=len(watched)) in summary
    stream = _stream(methods)
    star = next(i for i, t in enumerate(stream) if t.startswith("⭐"))
    assert all(not t.startswith("📅") for t in stream[star:])
    assert stream[star] == texts.WATCHLIST_HEADER.format(count=len(watched))
    for card in stream[star + 1 :]:
        if not card.startswith("⭐"):
            assert texts.CARD_WATCHLIST in card
            assert "Arsenal" in card


async def test_watchlist_only_result_says_value_not_found(
    admin: Client, services: Services
) -> None:
    arsenal = (await services.catalog.search_teams("Arsenal", sport=Sport.FOOTBALL))[0]
    await services.watchlist.add_watch(
        admin.user_id, WatchTarget(kind=WatchKind.TEAM, entity_id=arsenal.team_id)
    )
    await admin.send("/tahlil 7kun 95% ev20%")

    methods = await admin.press(texts.BTN_START)

    summary = next(t for t in shown(methods) if "Value topilmadi" in t)
    assert texts.RESULTS_SHORTFALL.split("{")[0] not in summary
    assert "⭐" in summary


# --------------------------------------------------------------------------- progress / flood
async def test_progress_edits_are_throttled_through_the_dispatcher(
    admin: Client, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(ProgressReporter.__init__, "__defaults__", (lambda: clock["now"],))

    async def analyze(user_id: int, query: AnalysisQuery, **kwargs: Any) -> AnalysisResult:
        progress = kwargs["progress"]
        result: AnalysisResult = await real(user_id, query)
        for done in range(1, 21):
            clock["now"] += 0.4
            await progress(AnalysisProgress(stage=AnalysisStage.ANALYSING, done=done, total=20))
        finished = result.fixtures_analyzed
        await progress(
            AnalysisProgress(stage=AnalysisStage.FINISHED, done=finished, total=finished)
        )
        analysed.append(finished)
        return result

    real = services.analysis.analyze
    analysed: list[int] = []
    await to_confirm(admin)
    services.analysis.analyze = analyze  # type: ignore[method-assign]

    methods = await admin.press(texts.BTN_START)

    progress_id = next(
        message_id
        for message_id, text in admin.texts.items()
        if text.startswith(("⏳", "✅ Tahlil tugadi"))
    )
    edits = [m for m in methods if isinstance(m, EditMessageText) and m.message_id == progress_id]
    # Edits at 1.6, 3.2, 4.8, 6.4, 8.0 s (≥ 1.5 s apart) plus the final stage.
    assert [edit.text for edit in edits] == [
        *(texts.PROGRESS_STAGES["analysing"].format(done=d, total=20) for d in (4, 8, 12, 16, 20)),
        texts.PROGRESS_STAGES["finished"].format(total=analysed[0]),
    ]
    assert PROGRESS_MIN_INTERVAL == 1.5


class FloodFirstSendSession(RecordingSession):
    """The first sendMessage hits flood control (retry after 2 s)."""

    def __init__(self) -> None:
        super().__init__()
        self.flooded = 0

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 - aiogram's signature
    ) -> Any:
        if isinstance(method, SendMessage) and not self.flooded:
            self.flooded += 1
            raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=2)
        return await super().make_request(bot, method, timeout)


async def test_flood_control_retries_once_and_user_gets_the_reply() -> None:
    session = FloodFirstSendSession()
    bot = Bot(token="42:TEST", session=session)
    sleep = AsyncMock()
    install_flood_control(bot, sleep)
    admin = Client(make_dispatcher(frozenset({ADMIN_ID})), bot, session, ADMIN_ID)

    methods = await admin.send("/start")

    sleep.assert_awaited_once_with(2.0)
    assert [m.text for m in sent(methods)] == [texts.WELCOME]
    assert answers(methods) == []
