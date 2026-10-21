"""QA: service errors → Uzbek messages, HTML escaping and Telegram-size splitting."""

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import SendMessage

from app.bot import texts
from app.bot.formatters import (
    TELEGRAM_TEXT_LIMIT,
    budget_message,
    cooldown_message,
    validation_message,
)
from app.bot.wizard import MAX_TEAM_NAMES_PER_MESSAGE
from app.domain import Sport
from app.services.errors import (
    BudgetExceededError,
    CooldownActiveError,
    NotFoundError,
    ValidationFailedError,
)
from app.services.interfaces import Services
from tests.e2e.bot_client import (
    Client,
    answers,
    sent,
    shown,
    telegram_html_problems,
)
from tests.e2e.flows import to_confirm


def assert_valid_html(methods: list[Any]) -> None:
    for text in shown(methods):
        assert telegram_html_problems(text) == [], text


# --------------------------------------------------------------------------- service errors
SERVICE_ERRORS = [
    pytest.param(
        CooldownActiveError(timedelta(minutes=2, seconds=5)),
        cooldown_message(CooldownActiveError(timedelta(minutes=2, seconds=5))),
        id="cooldown",
    ),
    pytest.param(
        BudgetExceededError(needed=45, available=30),
        budget_message(BudgetExceededError(needed=45, available=30)),
        id="budget",
    ),
    pytest.param(
        ValidationFailedError("date_to", "beyond horizon"),
        validation_message(ValidationFailedError("date_to", "beyond horizon")),
        id="validation-known-field",
    ),
    pytest.param(
        ValidationFailedError("weird_field", "x"),
        texts.VALIDATION_GENERIC,
        id="validation-unknown-field",
    ),
    pytest.param(NotFoundError("query", "q1"), texts.NOT_FOUND, id="not-found"),
]


def test_expected_uzbek_texts() -> None:
    assert cooldown_message(CooldownActiveError(timedelta(minutes=2, seconds=5))) == (
        texts.COOLDOWN.format(minutes=3)
    )
    assert "45 ta so'rov kerak" in budget_message(BudgetExceededError(needed=45, available=30))
    assert "30 ta qoldi" in budget_message(BudgetExceededError(needed=45, available=30))


@pytest.mark.parametrize(("error", "expected"), SERVICE_ERRORS)
async def test_service_error_during_analysis_is_explained_and_confirmation_returns(
    admin: Client, services: Services, error: Exception, expected: str
) -> None:
    await to_confirm(admin)
    services.analysis.analyze = AsyncMock(side_effect=error)  # type: ignore[method-assign]

    methods = await admin.press(texts.BTN_START)

    assert len(answers(methods)) == 1
    assert answers(methods)[0].text is None  # answered before the run, error as a message
    texts_shown = shown(methods)
    assert texts_shown[-1] == expected
    assert texts.CANCELLED in texts_shown  # the progress message is closed
    assert any("So'rov" in t and "Taxminiy API sarfi" in t for t in texts_shown)
    assert await admin.state() == "AnalysisFlow:confirm"
    assert admin.has_button(texts.BTN_START)  # the user can retry after narrowing
    assert_valid_html(methods)


@pytest.mark.parametrize(("error", "expected"), SERVICE_ERRORS)
async def test_service_error_on_a_card_button_is_an_alert(
    admin: Client, services: Services, error: Exception, expected: str
) -> None:
    await admin.send("/tahlil 3kun 50% ev3%")
    await admin.press(texts.BTN_START)
    services.signals.hide_signal = AsyncMock(side_effect=error)  # type: ignore[method-assign]

    methods = await admin.press(texts.BTN_HIDE)

    replies = answers(methods)
    assert len(replies) == 1
    if len(expected) <= 200:
        assert replies[0].show_alert is True
        assert replies[0].text == expected.replace("<b>", "").replace("</b>", "")
    else:
        assert shown(methods) == [expected]


async def test_unexpected_error_during_analysis_gets_generic_message(
    admin: Client, services: Services
) -> None:
    await to_confirm(admin)
    services.analysis.analyze = AsyncMock(side_effect=RuntimeError("db down"))  # type: ignore[method-assign]

    methods = await admin.press(texts.BTN_START)

    assert len(answers(methods)) == 1
    assert shown(methods)[-1] == texts.UNEXPECTED_ERROR
    assert "db down" not in " ".join(shown(methods))


@pytest.mark.xfail(
    strict=True,
    reason="F-2: an unexpected (non-ServiceError) failure in analyze leaves FSM state "
    "`running`; the analysis router then answers every later card/pagination button "
    "with 'eskirgan' until /start, /bekor or a new wizard",
)
async def test_unexpected_error_does_not_leave_the_user_stuck(
    admin: Client, services: Services
) -> None:
    await admin.send("/tahlil 3kun 50% ev3%")
    await admin.press(texts.BTN_START)
    card_message, hide = admin.find_button(texts.BTN_HIDE)
    await to_confirm(admin)
    services.analysis.analyze = AsyncMock(side_effect=RuntimeError("db down"))  # type: ignore[method-assign]
    await admin.press(texts.BTN_START)

    methods = await admin.press_data(hide, card_message)

    assert answers(methods)[0].text == texts.HIDDEN_ALERT


@pytest.mark.xfail(
    strict=True,
    reason="F-2: the progress message stays '⏳ Tahlil qilinmoqda… 0/…' forever after an "
    "unexpected failure (only ServiceError closes it)",
)
async def test_unexpected_error_closes_the_progress_message(
    admin: Client, services: Services
) -> None:
    await to_confirm(admin)
    services.analysis.analyze = AsyncMock(side_effect=RuntimeError("db down"))  # type: ignore[method-assign]

    await admin.press(texts.BTN_START)

    progress = texts.PROGRESS.format(done=0, total="…")
    assert progress not in admin.texts.values()


async def test_stale_draft_on_typed_input_is_explained(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)
    await admin.press(texts.BTN_TODAY)
    await admin.press(texts.SPORT_LABELS[Sport.FOOTBALL])
    await admin.press(texts.BTN_CUSTOM)
    # Simulate a storage wipe of the draft while the state survived.
    storage = admin.dispatcher.storage
    await storage.set_data(admin.storage_key(), {})

    methods = await admin.send("60")

    assert shown(methods) == [texts.STALE_BUTTON_HINT]


# --------------------------------------------------------------------------- HTML escaping
INJECTION = "<b>x</b>&<i>"


async def test_team_search_text_is_escaped(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)
    for label in (
        texts.BTN_TODAY,
        texts.SPORT_LABELS[Sport.FOOTBALL],
        "50%",
        "5%",
        texts.BTN_SCOPE_TEAMS,
    ):
        await admin.press(label)

    methods = await admin.send(f"Zzqq {INJECTION}, Arsenal")

    notice = shown(methods)[-1]
    assert "«Zzqq &lt;b&gt;x&lt;/b&gt;&amp;&lt;i&gt;»" in notice
    assert texts.WIZARD_TEAMS.format(count=1) in notice
    assert_valid_html(methods)


@pytest.mark.parametrize(
    "name",
    [INJECTION, "A & B <script>alert(1)</script>", "Dam olish · APL · 60%+ <>&"],
)
async def test_template_name_is_escaped(admin: Client, services: Services, name: str) -> None:
    await to_confirm(admin)
    await admin.press("Shablon")

    methods = await admin.send(name)

    assert_valid_html(methods)
    expected = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    assert texts.TEMPLATE_SAVED.format(name=expected) in shown(methods)[-1]
    saved = await services.templates.list_templates(admin.user_id)
    assert [template.name for template in saved] == [name]


@pytest.mark.parametrize(("name", "field"), [("x" * 65, "name"), ("   ", "name")])
async def test_bad_template_name_keeps_the_step(admin: Client, name: str, field: str) -> None:
    await to_confirm(admin)
    await admin.press("Shablon")

    methods = await admin.send(name)

    assert shown(methods) == [validation_message(ValidationFailedError(field, "bad"))]
    assert await admin.state() == "AnalysisFlow:template_name"


@pytest.mark.parametrize(
    "command",
    [f"/tahlil {INJECTION}", "/tahlil ertaga A&B <X>", "/tahlil 60 <b>"],
)
async def test_quick_command_echo_is_escaped(admin: Client, command: str) -> None:
    methods = await admin.send(command)

    assert len(sent(methods)) == 1
    assert_valid_html(methods)
    assert "Namuna:" in shown(methods)[0]


async def test_menu_placeholder_and_unknown_text_are_valid_html(admin: Client) -> None:
    for text in (texts.MENU_CUSTOM, "<b>", "&", "/unknown <x>"):
        assert_valid_html(await admin.send(text))


# --------------------------------------------------------------------------- 4096 split
def _unknown_team_names() -> str:
    """The most names one message may carry (10), each long and full of ``&``/``<``.

    Escaped, the "not found" line is one ~13 000-character line: it has to be cut inside.
    """
    names = ", ".join(f"{'&Q<' * 128}{index}" for index in range(MAX_TEAM_NAMES_PER_MESSAGE))
    assert len(names) <= TELEGRAM_TEXT_LIMIT
    return names


def _chunks(methods: list[Any]) -> list[str]:
    return [method.text for method in methods if isinstance(method, SendMessage)]


async def test_very_long_not_found_list_splits_into_valid_html(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)
    for label in (
        texts.BTN_TODAY,
        texts.SPORT_LABELS[Sport.FOOTBALL],
        "50%",
        "5%",
        texts.BTN_SCOPE_TEAMS,
    ):
        await admin.press(label)
    names = _unknown_team_names()

    methods = await admin.send(names)

    chunks = _chunks(methods)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert telegram_html_problems(chunk) == [], chunk[:80]


async def test_very_long_not_found_list_never_exceeds_the_limit(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)
    for label in (
        texts.BTN_TODAY,
        texts.SPORT_LABELS[Sport.FOOTBALL],
        "50%",
        "5%",
        texts.BTN_SCOPE_TEAMS,
    ):
        await admin.press(label)
    names = _unknown_team_names()

    methods = await admin.send(names)

    chunks = _chunks(methods)
    assert len(chunks) >= 2
    assert all(len(chunk) <= 4096 for chunk in chunks)
    # The keyboard rides on the last chunk only, and the step still works.
    markups = [method.reply_markup for method in methods if isinstance(method, SendMessage)]
    assert all(markup is None for markup in markups[:-1])
    assert markups[-1] is not None
    assert await admin.state() == "AnalysisFlow:teams"


async def test_long_result_set_every_message_is_valid_html(admin: Client) -> None:
    await admin.send("/tahlil 7kun hammasi 30% ev0% 100ta")
    methods = await admin.press(texts.BTN_START)
    for _ in range(30):
        if not admin.has_button("Yana"):
            break
        methods += await admin.press("Yana")

    assert len(_chunks(methods)) > 10
    assert_valid_html(methods)
