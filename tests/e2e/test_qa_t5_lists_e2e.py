"""QA (T5 bot): templates, watchlist, callback_data sizes, message splitting and texts."""

import re
import zlib
from datetime import time
from itertools import count

import pytest
from aiogram.filters.callback_data import CallbackData

from app.bot import callbacks, texts
from app.bot.callbacks import TplCb
from app.bot.formatters import split_message
from app.bot.template_ui import format_templates
from app.bot.watch_ui import entity_key, find_item, format_watchlist
from app.services.interfaces import (
    AnalysisQuery,
    DateSpec,
    TemplateDraft,
    TemplateSchedule,
    WatchItem,
    WatchKind,
    WatchTarget,
)
from tests.e2e.bot_client import Client, answers, buttons_of, shown, telegram_html_problems
from tests.e2e.real_world import ADMIN, RealBot
from tests.bot.fakes import ADMIN_ID
from tests.pipeline.world import TEAM_NAMES

QUERY = AnalysisQuery(dates=DateSpec.next_days(3), min_probability=0.4, min_ev=0.02)


async def _template(services: object, name: str = "Haftasonasi") -> int:
    saved = await services.templates.save_template(  # type: ignore[attr-defined]
        ADMIN_ID, TemplateDraft(name=name, query=QUERY)
    )
    return saved.template_id


# --------------------------------------------------------------------------- schedule editor
@pytest.mark.parametrize(
    "typed", ["25:00", "24:00", "9", "09:60", "ab", "0900", "09:00:00", "9:5", "-1:30", "09-00"]
)
async def test_an_invalid_schedule_time_keeps_the_editor_and_explains(
    admin: Client, services: object, typed: str
) -> None:
    template_id = await _template(services)
    await admin.send(texts.MENU_TEMPLATES)
    await admin.press("Haftasonasi")
    await admin.press(texts.BTN_TPL_SCHEDULE)
    await admin.press(texts.BTN_TPL_TYPE_TIME)
    assert await admin.state() == "TemplateFlow:time_input"

    reply = shown(await admin.send(typed))

    assert reply, "the user must get an explanation"
    assert await admin.state() == "TemplateFlow:time_input", "the prompt must stay open"
    saved = (await services.templates.list_templates(ADMIN_ID))[0]  # type: ignore[attr-defined]
    assert saved.template_id == template_id
    assert saved.schedule is None  # nothing was stored by the failed attempt


@pytest.mark.parametrize(("typed", "expected"), [("09:00", time(9)), ("0:00", time(0)), ("23:59", time(23, 59))])
async def test_a_valid_schedule_time_is_saved_for_the_chosen_days(
    admin: Client, services: object, typed: str, expected: time
) -> None:
    await _template(services)
    await admin.send(texts.MENU_TEMPLATES)
    await admin.press("Haftasonasi")
    await admin.press(texts.BTN_TPL_SCHEDULE)
    await admin.press(texts.BTN_TPL_TYPE_TIME)
    await admin.send(typed)
    await admin.press(texts.BTN_TPL_SAVE_SCHEDULE)

    saved = (await services.templates.list_templates(ADMIN_ID))[0]  # type: ignore[attr-defined]

    assert saved.schedule == TemplateSchedule(weekdays=frozenset({1, 2, 3, 4, 5}), at=expected)


async def test_a_schedule_without_any_day_cannot_be_saved(
    admin: Client, services: object
) -> None:
    await _template(services)
    await admin.send(texts.MENU_TEMPLATES)
    await admin.press("Haftasonasi")
    await admin.press(texts.BTN_TPL_SCHEDULE)
    for day in texts.WEEKDAYS_SHORT[:5]:
        await admin.press_option(day)  # switch the five default days off

    methods = await admin.press(texts.BTN_TPL_SAVE_SCHEDULE)

    assert answers(methods)[0].text == texts.TPL_PICK_A_DAY
    assert (await services.templates.list_templates(ADMIN_ID))[0].schedule is None  # type: ignore[attr-defined]


async def test_saving_a_schedule_twice_keeps_one_template(admin: Client, services: object) -> None:
    await _template(services)
    await admin.send(texts.MENU_TEMPLATES)
    await admin.press("Haftasonasi")
    await admin.press(texts.BTN_TPL_SCHEDULE)
    message_id, save = admin.find_button(texts.BTN_TPL_SAVE_SCHEDULE)

    await admin.press_data(save, message_id)
    await admin.press_data(save, message_id)  # double tap: the draft is gone, so stale

    assert len(await services.templates.list_templates(ADMIN_ID)) == 1  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- watchlist (real)
async def _team_names(rb: RealBot) -> list[tuple[str, str]]:
    """(team_id, name) of every team of the seeded world."""
    found: dict[str, str] = {}
    for names in TEAM_NAMES.values():
        for name in names:
            for team in await rb.services.catalog.search_teams(name, limit=1):
                found[team.team_id] = team.name
    return sorted(found.items())


@pytest.mark.slow
async def test_following_the_same_team_twice_by_command_keeps_one_entry(real_bot: RealBot) -> None:
    rb = real_bot
    (_, name) = (await _team_names(rb))[0]
    admin = rb.client()

    first = shown(await admin.send(f"/kuzat {name}"))
    second = shown(await admin.send(f"/kuzat {name.upper()}"))

    assert len(await rb.services.watchlist.list_watchlist(ADMIN)) == 1
    assert first and second


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason="QA F-7: the watchlist screen shows 20 buttons; entry 21+ can be neither opened "
    "nor removed from the bot (same for templates)",
)
async def test_every_followed_team_can_be_opened_from_the_list(real_bot: RealBot) -> None:
    rb = real_bot
    teams = await _team_names(rb)
    assert len(teams) > 20
    for team_id, _ in teams:
        await rb.services.watchlist.add_watch(
            ADMIN, WatchTarget(kind=WatchKind.TEAM, entity_id=team_id)
        )
    admin = rb.client()

    await admin.send(texts.MENU_WATCHLIST)

    opens = [d for _, d in admin.live_buttons(admin.last_message_id) if d.startswith("wl:open")]
    assert len(opens) == len(teams)


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason="QA F-7: 25 templates, only the first 20 (by name) get a button, so the rest "
    "can be neither run, scheduled nor deleted",
)
async def test_every_saved_template_has_a_button(real_bot: RealBot) -> None:
    rb = real_bot
    for number in range(25):
        await rb.services.templates.save_template(
            ADMIN, TemplateDraft(name=f"T{number:02d}", query=QUERY)
        )
    admin = rb.client()

    await admin.send(texts.MENU_TEMPLATES)

    opens = [d for _, d in admin.live_buttons(admin.last_message_id) if d.startswith("tp:open")]
    assert len(opens) == 25


def _crc_collision() -> tuple[str, str]:
    seen: dict[int, str] = {}
    for number in count():
        candidate = f"team:{number}"
        checksum = zlib.crc32(candidate.encode())
        if checksum in seen:
            return seen[checksum], candidate
        seen[checksum] = candidate
    raise AssertionError  # pragma: no cover


@pytest.mark.xfail(
    strict=True,
    reason="QA F-9: a watchlist button carries crc32(entity_id); two followed ids with the same "
    "crc32 (about 1 in 4e9 per pair, easy to construct) address the first entry for both",
)
def test_two_entities_with_the_same_crc_are_told_apart() -> None:
    first_id, second_id = _crc_collision()
    items = [
        WatchItem(
            target=WatchTarget(kind=WatchKind.TEAM, entity_id=entity),
            name=entity,
            sport=None,  # type: ignore[arg-type]
            notifications=frozenset(),
        )
        for entity in (first_id, second_id)
    ]
    assert entity_key(first_id) == entity_key(second_id)

    found = find_item(items, "t", entity_key(second_id))

    assert found is not None
    assert found.target.entity_id == second_id


# --------------------------------------------------------------------------- callback_data size
def _worst_cases() -> list[CallbackData]:
    big = 2**63 - 1
    return [
        callbacks.TplCb(action="delete_yes", id=big, value=big),
        callbacks.BankCb(action="mark", id=big, value="c"),
        callbacks.CardCb(action="recalc", signal_id=big),
        callbacks.BetCb(action="force", signal_id=big),
        callbacks.CustomCb(action="toggle", index=big),
        callbacks.ExpressCb(action="toggle", index=big),
        callbacks.ReportCb(action="chart", period="all", group="risk_profile"),
        callbacks.WatchCb(action="notify", kind="t", key="ffffffff", flag="o"),
        callbacks.WatchCb(action="card", kind="l", key=str(big)),
        callbacks.ResultsCb(page=big, token=999),
        callbacks.RecalcAllCb(kind="q", ref=callbacks.encode_ref("é" * 20)),
        callbacks.SettingsCb(field="weekly_loss", action="toggle", index=big, key="ffffffff"),
    ]


@pytest.mark.parametrize("data", _worst_cases(), ids=lambda d: type(d).__name__)
def test_the_largest_possible_callback_data_fits_telegrams_64_bytes(data: CallbackData) -> None:
    packed = data.pack()

    assert len(packed.encode()) <= 64
    assert type(data).unpack(packed) == data


@pytest.mark.parametrize("entity", ["😀" * 16, "a:b:c" * 12, "é" * 32, "x" * 64, "::::"])
def test_entity_keys_stay_8_hex_whatever_the_catalog_id_looks_like(entity: str) -> None:
    assert re.fullmatch(r"[0-9a-f]{8}", entity_key(entity))


async def test_keyboards_with_hostile_names_keep_every_callback_small_and_texts_valid(
    admin: Client, services: object
) -> None:
    evil = "<b>&\"'😀" * 8  # 64 chars of markup characters and emoji
    await services.templates.save_template(  # type: ignore[attr-defined]
        ADMIN_ID, TemplateDraft(name=evil[:64], query=QUERY)
    )

    methods = await admin.send(texts.MENU_TEMPLATES)

    for method in methods:
        markup = getattr(method, "reply_markup", None)
        for button in buttons_of(markup):
            if button.callback_data:
                assert len(button.callback_data.encode()) <= 64
    for text in shown(methods):
        assert telegram_html_problems(text) == []
    assert TplCb  # imported for the factory type


# --------------------------------------------------------------------------- long messages
def _item(entity: str, name: str) -> WatchItem:
    from app.domain import Sport  # noqa: PLC0415

    return WatchItem(
        target=WatchTarget(kind=WatchKind.TEAM, entity_id=entity),
        name=name,
        sport=Sport.FOOTBALL,
        notifications=frozenset(),
    )


@pytest.mark.parametrize("name", ["&" * 60, "<" * 60, "Ä" * 60])
def test_a_full_watchlist_of_hostile_names_splits_into_valid_html(name: str) -> None:
    items = [_item(f"{n}", f"{name}{n}") for n in range(20)]

    for part in split_message(format_watchlist(items)):
        assert telegram_html_problems(part) == [], part[:80]


async def test_a_full_template_list_of_long_names_is_valid_and_within_the_limit(
    admin: Client, services: object
) -> None:
    for number in range(20):
        await services.templates.save_template(  # type: ignore[attr-defined]
            ADMIN_ID, TemplateDraft(name=f"{number:02d}" + "&<" * 31, query=QUERY)
        )
    views = await services.templates.list_templates(ADMIN_ID)  # type: ignore[attr-defined]

    first_part = split_message(format_templates(views))[0]
    methods = await admin.send(texts.MENU_TEMPLATES)

    assert telegram_html_problems(first_part) == []
    for text in shown(methods):
        assert telegram_html_problems(text) == []


# --------------------------------------------------------------------------- texts
def _strings() -> dict[str, str]:
    return {k: v for k, v in vars(texts).items() if isinstance(v, str) and k.isupper()}


def test_texts_use_one_apostrophe_and_have_no_leftover_placeholders() -> None:
    odd_quotes = re.compile("[’ʻʼ`‘]")
    leftover = re.compile(
        r"tez orada|yaqin kunlarda|coming soon|todo|fixme|lorem|keyingi to'lqin", re.IGNORECASE
    )
    for name, value in _strings().items():
        assert not odd_quotes.search(value), f"{name}: non-ASCII apostrophe"
        assert not leftover.search(value), f"{name}: placeholder text"
        assert "{}" not in value, f"{name}: positional placeholder"


def test_texts_that_name_a_feature_as_missing_say_so_honestly() -> None:
    assert "ulanmagan" in texts.TPL_SCHEDULE_NOTE  # the schedule does not fire automatically


# --------------------------------------------------------------------------- money formatting
@pytest.mark.xfail(
    strict=True,
    reason="QA F-10: a tiny loss (a cash-out scaled to the stake) shows as '-0' on the bank screen",
)
def test_a_loss_smaller_than_a_cent_is_shown_as_plain_zero() -> None:
    from decimal import Decimal  # noqa: PLC0415

    from app.bot.bank_ui import signed_money  # noqa: PLC0415

    assert signed_money(Decimal("-0.004")) == "0"
