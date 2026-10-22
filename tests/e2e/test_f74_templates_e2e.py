"""💾 Shablonlarim through the real dispatcher over the fake services (F7.4)."""

from datetime import time

from app.bot import texts
from app.bot.callbacks import TplCb
from app.bot.draft import WizardDraft
from app.bot.states import TemplateFlow
from app.bot.template_ui import ScheduleDraft, date_label, days_label, schedule_label
from app.domain import MarketType
from app.services.interfaces import (
    AnalysisQuery,
    DateSpec,
    ScopeKind,
    Services,
    SortKey,
    TemplateDraft,
    TemplateSchedule,
    TemplateView,
)
from tests.bot.fakes import ADMIN_ID
from tests.e2e.bot_client import Client, answers, shown, telegram_html_problems
from tests.e2e.conftest import SECOND_ADMIN_ID


async def save(services: Services, name: str = "Dam olish", **query: object) -> TemplateView:
    base: dict[str, object] = {
        "dates": DateSpec.next_days(3),
        "min_probability": 0.5,
        "min_ev": 0.03,
        "markets": frozenset({MarketType.MATCH_RESULT, MarketType.TOTALS}),
        "max_signals": 5,
    }
    return await services.templates.save_template(
        ADMIN_ID, TemplateDraft(name=name, query=AnalysisQuery(**{**base, **query}))
    )  # type: ignore[arg-type]


async def stored(services: Services, name: str = "Dam olish") -> TemplateView:
    views = await services.templates.list_templates(ADMIN_ID)
    return next(view for view in views if view.name == name)


# --------------------------------------------------------------------------- saving and listing
async def test_empty_list_tells_where_to_save(admin: Client) -> None:
    methods = await admin.send(texts.MENU_TEMPLATES)

    assert shown(methods) == [texts.TPL_EMPTY]


async def test_a_template_saved_from_the_wizard_is_listed(admin: Client) -> None:
    await admin.send("/tahlil 3kun 60% ev4% 5ta")
    await admin.press(texts.BTN_SAVE_TEMPLATE)
    await admin.send("APL <b>x</b> & Co")

    methods = await admin.send(texts.MENU_TEMPLATES)

    (text,) = shown(methods)
    assert "APL &lt;b&gt;x&lt;/b&gt; &amp; Co" in text
    assert "3 kun · ⚽ Futbol · ≥ 60% · EV ≥ 4%" in text
    assert telegram_html_problems(text) == []


async def test_list_marks_scheduled_templates(admin: Client, services: Services) -> None:
    view = await save(services)
    await services.templates.save_template(
        ADMIN_ID,
        TemplateDraft(
            name=view.name,
            query=view.query,
            schedule=TemplateSchedule(weekdays=frozenset({1, 3}), at=time(8, 30)),
        ),
    )

    methods = await admin.send(texts.MENU_TEMPLATES)

    assert "🗓 Du Ch 08:30" in shown(methods)[0]
    assert admin.has_button("🗓 Dam olish")


# --------------------------------------------------------------------------- run
async def test_running_a_template_opens_the_confirmation_with_its_query(
    admin: Client, services: Services
) -> None:
    await save(services)
    await admin.send(texts.MENU_TEMPLATES)

    methods = await admin.press("Dam olish")
    assert "📅 3 kun · ⚽ Futbol" in shown(methods)[0]
    methods = await admin.press(texts.BTN_TPL_RUN)

    text = shown(methods)[0]
    assert "Shablon: <b>Dam olish</b>" in text
    assert "≥ 50%" in text and "EV ≥ 3%" in text
    assert "Soni:</b> 5" in text
    assert admin.has_button(texts.BTN_START)
    assert admin.has_button(texts.BTN_EDIT)


async def test_a_template_run_gives_results_like_the_wizard(
    admin: Client, services: Services
) -> None:
    await save(services, min_probability=0.3, min_ev=0.0)
    await admin.send(texts.MENU_TEMPLATES)
    await admin.press("Dam olish")
    await admin.press(texts.BTN_TPL_RUN)

    methods = await admin.press(texts.BTN_START)

    assert any("Natijalar" in text for text in shown(methods))


async def test_a_template_keeps_custom_analysis_and_scope(
    admin: Client, services: Services
) -> None:
    leagues = await services.catalog.list_leagues((await services.catalog.list_sports())[0].sport)
    await save(
        services,
        name="APL",
        scope=ScopeKind.LEAGUES,
        scope_ids=frozenset({leagues[0].league_id}),
        include_no_value=True,
        sort=SortKey.KICKOFF,
        per_day=True,
    )
    await admin.send(texts.MENU_TEMPLATES)
    await admin.press("APL")
    methods = await admin.press(texts.BTN_TPL_RUN)

    text = shown(methods)[0]
    assert leagues[0].name in text
    assert "(kuniga)" in text
    data = await admin.dispatcher.storage.get_data(admin.storage_key())
    query = WizardDraft.from_data(data["draft"]).to_query()
    assert query.include_no_value is True
    assert query.sort is SortKey.KICKOFF


async def test_running_a_template_with_a_stale_range_explains(
    admin: Client, services: Services
) -> None:
    await save(services, dates=DateSpec.between(*_old_range()))
    await admin.send(texts.MENU_TEMPLATES)
    await admin.press("Dam olish")

    methods = await admin.press(texts.BTN_TPL_RUN)

    assert answers(methods)[0].text == texts.PARSE_TOO_OLD.format(max_lookback=90)


def _old_range() -> tuple[object, object]:
    from datetime import date  # noqa: PLC0415

    return date(2026, 1, 1), date(2026, 1, 5)


# --------------------------------------------------------------------------- delete
async def test_delete_asks_first_and_can_be_declined(admin: Client, services: Services) -> None:
    await save(services)
    await admin.send(texts.MENU_TEMPLATES)
    await admin.press("Dam olish")

    methods = await admin.press(texts.BTN_TPL_DELETE)
    assert "o'chirilsinmi" in shown(methods)[0]
    await admin.press(texts.BTN_TPL_DELETE_NO)

    assert len(await services.templates.list_templates(ADMIN_ID)) == 1


async def test_confirmed_delete_removes_it(admin: Client, services: Services) -> None:
    await save(services)
    await admin.send(texts.MENU_TEMPLATES)
    await admin.press("Dam olish")
    await admin.press(texts.BTN_TPL_DELETE)

    methods = await admin.press(texts.BTN_TPL_DELETE_YES)

    assert await services.templates.list_templates(ADMIN_ID) == ()
    assert "Shablon o'chirildi" in shown(methods)[0]
    assert texts.TPL_EMPTY in shown(methods)[0]


async def test_buttons_of_a_deleted_template_say_not_found(
    admin: Client, services: Services
) -> None:
    view = await save(services)
    await admin.send(texts.MENU_TEMPLATES)
    message_id, data = admin.find_button("Dam olish")
    await services.templates.delete_template(ADMIN_ID, view.template_id)

    methods = await admin.press_data(data, message_id)

    assert texts.NOT_FOUND in [a.text for a in answers(methods)] + shown(methods)


async def test_templates_are_private_to_their_user(admin: Client, services: Services) -> None:
    view = await save(services)
    other = Client(admin.dispatcher, admin.bot, admin.session, SECOND_ADMIN_ID)

    listing = await other.send(texts.MENU_TEMPLATES)
    assert shown(listing) == [texts.TPL_EMPTY]
    methods = await other.press_data(TplCb(action="run", id=view.template_id).pack(), 1)
    assert texts.NOT_FOUND in [a.text for a in answers(methods)] + shown(methods)
    methods = await other.press_data(TplCb(action="delete_yes", id=view.template_id).pack(), 1)
    assert len(await services.templates.list_templates(ADMIN_ID)) == 1
    assert methods


# --------------------------------------------------------------------------- schedule
async def open_schedule(admin: Client, services: Services) -> TemplateView:
    view = await save(services)
    await admin.send(texts.MENU_TEMPLATES)
    await admin.press("Dam olish")
    await admin.press(texts.BTN_TPL_SCHEDULE)
    return view


async def test_schedule_editor_starts_on_working_days_at_the_digest_time(
    admin: Client, services: Services
) -> None:
    await open_schedule(admin, services)

    text = admin.last_text
    assert "Du Se Ch Pa Ju" in text
    assert "09:00" in text
    assert texts.TPL_SCHEDULE_NOTE in text
    assert admin.has_button("✅ Du")
    assert admin.has_button("▫️ Sh")


async def test_schedule_can_be_edited_and_saved(admin: Client, services: Services) -> None:
    await open_schedule(admin, services)
    await admin.press_option("Sh")  # Saturday on
    await admin.press_option("Du")  # Monday off
    await admin.press("08:00")

    methods = await admin.press(texts.BTN_TPL_SAVE_SCHEDULE)

    view = await stored(services)
    assert view.schedule == TemplateSchedule(weekdays=frozenset({2, 3, 4, 5, 6}), at=time(8, 0))
    text = shown(methods)[0]
    assert "Jadval saqlandi" in text
    assert "Se Ch Pa Ju Sh, 08:00" in text
    assert await admin.state() is None


async def test_schedule_with_no_day_cannot_be_saved(admin: Client, services: Services) -> None:
    await open_schedule(admin, services)
    for day in ("Du", "Se", "Ch", "Pa", "Ju"):
        await admin.press_option(day)

    methods = await admin.press(texts.BTN_TPL_SAVE_SCHEDULE)

    assert answers(methods)[0].text == texts.TPL_PICK_A_DAY
    assert (await stored(services)).schedule is None


async def test_a_typed_time_is_validated_and_applied(admin: Client, services: Services) -> None:
    await open_schedule(admin, services)
    await admin.press(texts.BTN_TPL_TYPE_TIME)
    assert await admin.state() == TemplateFlow.time_input.state

    bad = await admin.send("25:99")
    assert "vaqt emas" in shown(bad)[0]
    assert await admin.state() == TemplateFlow.time_input.state

    good = await admin.send("7:45")
    assert "<b>07:45</b>" in shown(good)[0]
    await admin.press(texts.BTN_TPL_SAVE_SCHEDULE)
    assert (await stored(services)).schedule == TemplateSchedule(
        weekdays=frozenset({1, 2, 3, 4, 5}), at=time(7, 45)
    )


async def test_clearing_removes_the_schedule(admin: Client, services: Services) -> None:
    view = await save(services)
    await services.templates.save_template(
        ADMIN_ID,
        TemplateDraft(
            name=view.name,
            query=view.query,
            schedule=TemplateSchedule(weekdays=frozenset({6}), at=time(10)),
        ),
    )
    await admin.send(texts.MENU_TEMPLATES)
    await admin.press("Dam olish")

    methods = await admin.press(texts.BTN_TPL_SCHEDULE)
    assert "Sh" in shown(methods)[0]
    await admin.press(texts.BTN_TPL_CLEAR_SCHEDULE)

    assert (await stored(services)).schedule is None
    assert (await stored(services)).template_id == view.template_id  # replaced, not duplicated


async def test_old_schedule_buttons_are_stale(admin: Client, services: Services) -> None:
    view = await open_schedule(admin, services)
    old_id, day = admin.find_button("Sh")
    await admin.send(texts.MENU_TEMPLATES)  # leaves the editor

    methods = await admin.press_data(day, old_id)

    assert "Bu tugma eskirgan" in (answers(methods)[0].text or "")
    assert view.template_id == TplCb.unpack(day).id


async def test_a_schedule_button_for_another_template_is_stale(
    admin: Client, services: Services
) -> None:
    await open_schedule(admin, services)
    message_id, day = admin.find_button("Sh")
    other = TplCb(action="day", id=TplCb.unpack(day).id + 1, value=6).pack()

    methods = await admin.press_data(other, message_id)

    assert methods  # refused: either not found or stale, never applied
    assert (await stored(services)).schedule is None


# --------------------------------------------------------------------------- pure helpers
def test_labels() -> None:
    assert date_label(DateSpec.today()) == "bugun"
    assert date_label(DateSpec.weekend()) == "dam olish kunlari"
    assert date_label(DateSpec.next_days(7)) == "7 kun"
    assert days_label([3, 1, 1]) == "Du Ch"
    assert days_label([]) == texts.TPL_NO_DAYS
    assert schedule_label(None) == texts.TPL_NO_SCHEDULE


def test_schedule_draft_validation() -> None:
    import pytest  # noqa: PLC0415

    from app.bot.draft import StaleDraftError  # noqa: PLC0415

    draft = ScheduleDraft(message_id=1, template_id=2, weekdays=(1, 2), at="09:00")
    assert ScheduleDraft.from_data(draft.to_data()) == draft
    assert draft.toggled(7).weekdays == (1, 2, 7)
    for bad in (None, {}, {**draft.to_data(), "at": "25:00"}, {**draft.to_data(), "weekdays": [9]}):
        with pytest.raises(StaleDraftError):
            ScheduleDraft.from_data(bad)
    with pytest.raises(StaleDraftError):
        draft.toggled(0)
    assert ScheduleDraft(message_id=1, template_id=2, weekdays=(), at="09:00").schedule() is None
