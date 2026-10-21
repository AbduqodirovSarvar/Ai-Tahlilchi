"""End-to-end (offline) tests of ⚙️ Sozlamalar."""

from dataclasses import replace
from datetime import time
from decimal import Decimal

import pytest

from app.bot import texts
from app.bot.callbacks import SettingsCb
from app.domain import MarketType, Sport
from app.services.interfaces import LeagueOption, QuietHours, RiskProfile, UserSettingsView
from tests.bot.fakes import ADMIN_ID, BotHarness, answers, sent_texts


async def current(harness: BotHarness) -> UserSettingsView:
    return await harness.services.settings.get_settings(ADMIN_ID)


async def open_field(harness: BotHarness, field: str) -> None:
    await harness.send(texts.MENU_SETTINGS)
    await harness.press(texts.SETTINGS_BUTTONS[field])


async def test_overview_lists_every_field(harness: BotHarness) -> None:
    methods = await harness.send(texts.MENU_SETTINGS)

    text = sent_texts(methods)[0]
    assert text.startswith("⚙️")
    for key in texts.SETTINGS_ROWS:
        assert texts.SETTINGS_ROWS[key].split(":")[0] in text
    assert "45%" in text  # balanced default
    assert "Asia/Tashkent" in text


async def test_settings_command_opens_overview(harness: BotHarness) -> None:
    methods = await harness.send("/sozlamalar")
    assert sent_texts(methods)[0].startswith("⚙️")


@pytest.mark.parametrize(
    ("field", "label", "check"),
    [
        ("max_signals", "15", lambda s: s.max_signals == 15),
        ("min_ev", "8%", lambda s: s.min_ev == 0.08),
        ("odds", "1.40–3.00", lambda s: (s.odds_min, s.odds_max) == (1.40, 3.00)),
        ("send_time", "07:00", lambda s: s.send_time == time(7)),
        ("prematch", texts.OFF, lambda s: s.prematch_minutes is None),
        ("quiet", "23:00–07:00", lambda s: s.quiet_hours == QuietHours(time(23), time(7))),
        ("timezone", "Europe/Moscow", lambda s: s.timezone == "Europe/Moscow"),
        ("min_probability", "60%", lambda s: s.min_probability == 0.60),
    ],
)
async def test_preset_choice_is_saved(harness: BotHarness, field: str, label: str, check) -> None:  # type: ignore[no-untyped-def]
    await open_field(harness, field)

    methods = await harness.press(label)

    assert answers(methods)[0].text == texts.SETTINGS_SAVED
    assert check(await current(harness))
    assert sent_texts(methods)[-1].startswith("⚙️")


async def test_risk_profile_applies_preset(harness: BotHarness) -> None:
    await open_field(harness, "risk")
    await harness.press(texts.RISK_LABELS[RiskProfile.CONSERVATIVE])

    settings = await current(harness)
    assert settings.risk_profile is RiskProfile.CONSERVATIVE
    assert (settings.min_probability, settings.min_ev) == (0.60, 0.03)


async def test_toggle_sports_and_markets(harness: BotHarness) -> None:
    await open_field(harness, "sports")
    await harness.press("Xokkey")
    assert (await current(harness)).sports == {Sport.FOOTBALL, Sport.HOCKEY}

    await harness.press(texts.BTN_SETTINGS_BACK)
    await harness.press(texts.SETTINGS_BUTTONS["markets"])
    await harness.press("Ikkala jamoa")
    assert MarketType.BTTS in (await current(harness)).markets


async def test_last_sport_cannot_be_removed(harness: BotHarness) -> None:
    await open_field(harness, "sports")

    methods = await harness.press("Futbol")

    assert answers(methods)[0].show_alert is True
    assert "Kamida bitta sport" in (answers(methods)[0].text or "")
    assert (await current(harness)).sports == {Sport.FOOTBALL}


async def test_leagues_toggle(harness: BotHarness) -> None:
    await open_field(harness, "leagues")
    await harness.press("Premier League")

    assert (await current(harness)).league_ids == {"af:l39"}
    overview = await harness.press(texts.BTN_SETTINGS_BACK)
    assert "Premier League" in sent_texts(overview)[-1]


async def test_markets_toggle_names_its_item_not_a_position(harness: BotHarness) -> None:
    """The keyboard was drawn for football; the sport changed before the press."""
    await open_field(harness, "markets")
    message_id, old_btts = harness.find_button("Ikkala jamoa")
    assert SettingsCb.unpack(old_btts).key == MarketType.BTTS.value
    settings = await current(harness)
    await harness.services.settings.update_settings(
        ADMIN_ID,
        replace(settings, sports=frozenset({Sport.HOCKEY}), markets=frozenset({MarketType.TOTALS})),
    )

    methods = await harness.press_data(old_btts, message_id)

    assert "eskirgan" in (answers(methods)[0].text or "")
    assert (await current(harness)).markets == {MarketType.TOTALS}


async def test_leagues_toggle_rejects_a_shifted_list(
    harness: BotHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await open_field(harness, "leagues")
    message_id, premier = harness.find_button("Premier League")
    real = harness.services.catalog.list_leagues

    async def with_new_league_first(sport: Sport) -> tuple[LeagueOption, ...]:
        extra = LeagueOption(league_id="af:l1", sport=sport, name="Aaa Liga")
        return (extra, *await real(sport))

    monkeypatch.setattr(harness.services.catalog, "list_leagues", with_new_league_first)

    methods = await harness.press_data(premier, message_id)

    assert "eskirgan" in (answers(methods)[0].text or "")
    assert (await current(harness)).league_ids == frozenset()


async def test_toggle_buttons_answer_exactly_once(harness: BotHarness) -> None:
    await open_field(harness, "sports")

    methods = await harness.press("Xokkey")

    assert [a.text for a in answers(methods)] == [texts.SETTINGS_SAVED]


async def test_typed_value_invalid_then_valid(harness: BotHarness) -> None:
    await open_field(harness, "min_ev")
    await harness.press(texts.BTN_CUSTOM)

    invalid = await harness.send("35")
    assert "0–20%" in sent_texts(invalid)[0]

    valid = await harness.send("4,5")
    assert sent_texts(valid)[0].startswith(texts.SETTINGS_SAVED)
    assert (await current(harness)).min_ev == 0.045


async def test_bank_unit_and_limits(harness: BotHarness) -> None:
    await open_field(harness, "bank")
    await harness.press(texts.BTN_CUSTOM)
    await harness.send("1 000 000")
    await harness.press(texts.SETTINGS_BUTTONS["unit"])
    await harness.press(texts.BTN_CUSTOM)

    too_big = await harness.send("2000000")
    assert "Unit bank summasidan oshmasligi" in sent_texts(too_big)[0]

    await harness.send("10000")
    settings = await current(harness)
    assert (settings.bank, settings.unit) == (Decimal(1000000), Decimal(10000))

    await harness.press(texts.SETTINGS_BUTTONS["daily_loss"])
    await harness.press(texts.BTN_CUSTOM)
    await harness.send("50000")
    assert (await current(harness)).daily_loss_limit == Decimal(50000)
    await harness.press(texts.SETTINGS_BUTTONS["daily_loss"])
    await harness.press(texts.BTN_OFF)
    assert (await current(harness)).daily_loss_limit is None


async def test_high_probability_setting_warns_once(harness: BotHarness) -> None:
    await open_field(harness, "min_probability")
    first = await harness.press("90%")
    assert "Diqqat" in sent_texts(first)[-1]

    await harness.press(texts.SETTINGS_BUTTONS["min_probability"])
    await harness.press(texts.BTN_CUSTOM)
    second = await harness.send("85")
    assert "Diqqat" not in sent_texts(second)[-1]


async def test_quiet_hours_typed_and_bad_time(harness: BotHarness) -> None:
    await open_field(harness, "quiet")
    await harness.press(texts.BTN_CUSTOM)

    bad = await harness.send("25:00-07:00")
    assert "Format" in sent_texts(bad)[0]

    await harness.send("22:30-06:30")
    assert (await current(harness)).quiet_hours == QuietHours(time(22, 30), time(6, 30))


async def test_unknown_settings_button_is_stale(harness: BotHarness) -> None:
    await harness.send(texts.MENU_SETTINGS)
    message_id, _ = harness.find_button(texts.SETTINGS_BUTTONS["bank"])

    methods = await harness.press_data(SettingsCb(field="zzz").pack(), message_id)
    assert "eskirgan" in (answers(methods)[0].text or "")

    methods = await harness.press_data(
        SettingsCb(field="max_signals", action="set", index=99).pack(), message_id
    )
    assert "eskirgan" in (answers(methods)[0].text or "")


async def test_typed_input_after_restart_is_ignored_gracefully(harness: BotHarness) -> None:
    await open_field(harness, "bank")
    await harness.press(texts.BTN_CUSTOM)
    harness.restart()

    methods = await harness.send("1000")

    assert sent_texts(methods) == [texts.UNKNOWN_TEXT]
