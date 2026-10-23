"""Real catalog, settings, template, watchlist and status services."""

from datetime import UTC, datetime, timedelta
from typing import Final

from app.domain import MarketType, Sport, normalize_name
from app.pipeline import data
from app.pipeline.provider import (
    DEFAULT_DAILY_LIMIT,
    DEFAULT_MANUAL_RESERVE,
    SUPPORTED_SPORT_MARKETS,
)
from app.pipeline.redaction import scrub_text
from app.pipeline.review import CandidateReviewer, llm_usage
from app.services.errors import NotFoundError, ValidationFailedError
from app.services.interfaces import (
    MAX_SEARCH_RESULTS,
    MIN_SEARCH_LENGTH,
    AnalysisQuery,
    ApiQuota,
    DateMode,
    DateSpec,
    ErrorEntry,
    FixtureListRequest,
    FixtureOption,
    FixturePage,
    JobStatus,
    LeagueOption,
    LearningMode,
    LlmQuota,
    RiskProfile,
    ScopeKind,
    SortKey,
    SportOption,
    SystemStatus,
    TeamOption,
    TemplateDraft,
    TemplateId,
    TemplateSchedule,
    TemplateView,
    UserSettingsView,
    WatchItem,
    WatchKind,
    WatchNotification,
    WatchTarget,
)
from app.services.real.analysis import window_of
from app.services.real.core import (
    EXPERIMENTAL_SPORTS,
    Core,
    parse_id,
    parse_ids,
    settings_record,
    settings_view,
)
from app.services.real.learning import mode_of

SEARCH_DAYS: Final = 366
LEARNING_OVERDUE_AFTER: Final = timedelta(days=8)
"""A weekly review that last ran longer ago than this (a week plus a day of grace) is overdue."""


class RealCatalogService:
    """:class:`~app.services.interfaces.CatalogService` over stored leagues, teams, fixtures."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    async def list_sports(self) -> tuple[SportOption, ...]:
        """See :meth:`CatalogService.list_sports`."""
        return tuple(
            SportOption(
                sport=sport,
                available=sport in SUPPORTED_SPORT_MARKETS,
                experimental=sport in EXPERIMENTAL_SPORTS,
                supported_markets=SUPPORTED_SPORT_MARKETS.get(sport, frozenset[MarketType]()),
            )
            for sport in Sport
        )

    async def list_leagues(self, sport: Sport) -> tuple[LeagueOption, ...]:
        """See :meth:`CatalogService.list_leagues` (one entry per competition)."""
        directory = self._core.ctx.leagues
        async with self._core.ctx.sessions() as session:
            rows = await data.list_leagues(session, sport)
        chosen = {}
        for league_id, league in rows:
            key = directory.canonical(league.ref)
            if key not in chosen or league.ref == key:
                chosen[key] = (league_id, league)
        options = [
            LeagueOption(
                league_id=str(league_id),
                sport=league.sport,
                name=league.name,
                country=league.country,
            )
            for league_id, league in chosen.values()
        ]
        return tuple(sorted(options, key=lambda option: option.name))

    async def search_teams(
        self, text: str, *, sport: Sport | None = None, limit: int = 10
    ) -> tuple[TeamOption, ...]:
        """See :meth:`CatalogService.search_teams`."""
        _check_search(text, limit)
        async with self._core.ctx.sessions() as session:
            hits = await data.search_teams(session, text, sport=sport, limit=limit)
        return tuple(
            TeamOption(
                team_id=str(hit.team_id), sport=hit.sport, name=hit.name, league=hit.league or ""
            )
            for hit in hits
        )

    async def search_fixtures(self, text: str, *, limit: int = 10) -> tuple[FixtureOption, ...]:
        """See :meth:`CatalogService.search_fixtures`."""
        tokens = _check_search(text, limit).split()
        now = self._core.now()
        async with self._core.ctx.sessions() as session:
            page = await data.list_fixtures(
                session, start=now, end=now + timedelta(days=SEARCH_DAYS)
            )
        found = []
        for record in page.items:
            label = normalize_name(f"{record.fixture.home.name} {record.fixture.away.name}")
            if record.fixture.kickoff > now and all(token in label for token in tokens):
                found.append(_option(record))
        return tuple(found[:limit])

    async def list_fixtures(self, user_id: int, request: FixtureListRequest) -> FixturePage:
        """See :meth:`CatalogService.list_fixtures`."""
        core = self._core
        profile = await core.profile(user_id)
        window = window_of(core, request.dates, profile)
        start, end = window.utc_bounds(profile.zone)
        now = core.now()
        async with core.ctx.sessions() as session:
            page = await data.list_fixtures(session, start=start, end=end)
            wanted = parse_ids(request.league_ids, "league_ids") | frozenset(
                profile.settings.leagues
            )
            leagues = await data.league_refs(session, wanted)
            teams = await data.team_refs(session, parse_ids(request.team_ids, "team_ids"))
        canonical = core.ctx.leagues.canonical
        explicit = bool(request.league_ids or request.team_ids)
        chosen_leagues = {
            canonical(leagues[i])
            for i in (
                parse_ids(request.league_ids, "league_ids")
                if explicit
                else profile.settings.leagues
            )
            if i in leagues
        }
        chosen_teams = set(teams.values())
        items = []
        for record in page.items:
            fixture = record.fixture
            if fixture.sport not in request.sports or _in_play(record, now):
                continue
            if explicit or chosen_leagues:
                hit = canonical(fixture.league.ref) in chosen_leagues or bool(
                    {fixture.home.ref, fixture.away.ref} & chosen_teams
                )
                if not hit:
                    continue
            items.append(_option(record))
        size = request.page_size
        first = request.page * size
        return FixturePage(
            items=tuple(items[first : first + size]),
            page=request.page,
            page_count=-(-len(items) // size),
            total=len(items),
        )


def _in_play(record: data.FixtureRecord, now: datetime) -> bool:
    from app.domain import FixtureStatus  # noqa: PLC0415

    fixture = record.fixture
    if fixture.status is FixtureStatus.LIVE:
        return True
    return fixture.status is FixtureStatus.SCHEDULED and fixture.kickoff <= now


def _option(record: data.FixtureRecord) -> FixtureOption:
    fixture = record.fixture
    return FixtureOption(
        fixture_id=str(record.id),
        sport=fixture.sport,
        league=fixture.league.name,
        home_team=fixture.home.name,
        away_team=fixture.away.name,
        kickoff=fixture.kickoff,
    )


def _check_search(text: str, limit: int) -> str:
    """Normalized search text; short text or a bad limit is a bot input error."""
    normalized = normalize_name(text)
    if len(normalized) < MIN_SEARCH_LENGTH:
        raise ValidationFailedError("text", f"type at least {MIN_SEARCH_LENGTH} characters")
    if not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise ValidationFailedError("limit", f"must be within [1, {MAX_SEARCH_RESULTS}]")
    return normalized


class RealSettingsService:
    """:class:`~app.services.interfaces.SettingsService`."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    async def get_settings(self, user_id: int) -> UserSettingsView:
        """See :meth:`SettingsService.get_settings`."""
        return settings_view(await self._core.profile(user_id))

    async def update_settings(self, user_id: int, settings: UserSettingsView) -> UserSettingsView:
        """See :meth:`SettingsService.update_settings`."""
        record = settings_record(user_id, settings)
        async with self._core.ctx.sessions.begin() as session:
            await data.ensure_user(session, user_id, timezone=settings.timezone)
            await data.update_user(
                session, user_id, timezone=settings.timezone, bank=settings.bank, unit=settings.unit
            )
            await data.upsert_user_settings(session, record)
        return await self.get_settings(user_id)

    async def apply_risk_profile(self, user_id: int, profile: RiskProfile) -> UserSettingsView:
        """See :meth:`SettingsService.apply_risk_profile`."""
        current = await self.get_settings(user_id)
        return await self.update_settings(user_id, current.with_risk_profile(profile))


class RealTemplateService:
    """:class:`~app.services.interfaces.TemplateService`."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    async def save_template(self, user_id: int, draft: TemplateDraft) -> TemplateView:
        """See :meth:`TemplateService.save_template`."""
        query, dates, schedule = draft.query, draft.query.dates, draft.schedule
        saved = data.SavedQueryDraft(
            user_id=user_id,
            name=draft.name.strip(),
            date_mode=data.DateMode(dates.mode.value),
            days=dates.days,
            date_from=dates.date_from,
            date_to=dates.date_to,
            sports=tuple(sorted(sport.value for sport in query.sports)),
            min_prob=query.min_probability,
            min_ev=query.min_ev,
            scope=data.QueryScope(query.scope.value),
            scope_ids=tuple(sorted(parse_ids(query.scope_ids, "scope_ids"))),
            markets=tuple(sorted(market.value for market in query.markets)),
            max_signals=query.max_signals,
            sort=data.SortOrder(query.sort.value),
            per_day=query.per_day,
            include_no_value=query.include_no_value,
            schedule_weekdays=tuple(sorted(schedule.weekdays)) if schedule else (),
            schedule_at=schedule.at if schedule else None,
        )
        async with self._core.ctx.sessions.begin() as session:
            await data.ensure_user(session, user_id, timezone=(await self._tz(user_id)))
            record = await data.save_query(session, saved)
        return _template(record)

    async def _tz(self, user_id: int) -> str:
        return (await self._core.profile(user_id)).zone.key

    async def list_templates(self, user_id: int) -> tuple[TemplateView, ...]:
        """See :meth:`TemplateService.list_templates`."""
        async with self._core.ctx.sessions() as session:
            records = await data.list_saved_queries(session, user_id)
        views = [_template(record) for record in records]
        return tuple(sorted(views, key=lambda view: view.name.casefold()))

    async def delete_template(self, user_id: int, template_id: TemplateId) -> None:
        """See :meth:`TemplateService.delete_template`."""
        async with self._core.ctx.sessions.begin() as session:
            deleted = await data.delete_saved_query(session, user_id, template_id)
        if not deleted:
            raise NotFoundError("template", template_id)


def _template(record: data.SavedQueryRecord) -> TemplateView:
    saved = record.query
    schedule = (
        None
        if saved.schedule_at is None
        else TemplateSchedule(weekdays=frozenset(saved.schedule_weekdays), at=saved.schedule_at)
    )
    query = AnalysisQuery(
        dates=DateSpec(
            mode=DateMode(saved.date_mode.value),
            days=saved.days,
            date_from=saved.date_from,
            date_to=saved.date_to,
        ),
        sports=frozenset(Sport(value) for value in saved.sports),
        min_probability=saved.min_prob,
        min_ev=saved.min_ev,
        scope=ScopeKind(saved.scope.value),
        scope_ids=frozenset(str(i) for i in saved.scope_ids),
        markets=frozenset(MarketType(value) for value in saved.markets),
        max_signals=saved.max_signals,
        per_day=saved.per_day,
        sort=SortKey(saved.sort.value),
        include_no_value=saved.include_no_value,
    )
    return TemplateView(template_id=record.id, name=saved.name, query=query, schedule=schedule)


class RealWatchlistService:
    """:class:`~app.services.interfaces.WatchlistService`."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    async def _entity(self, target: WatchTarget) -> tuple[int, str, Sport]:
        try:
            number = int(target.entity_id)
        except ValueError:
            raise NotFoundError(target.kind.value, target.entity_id) from None
        async with self._core.ctx.sessions() as session:
            found = (
                await data.get_team(session, number)
                if target.kind is WatchKind.TEAM
                else await data.get_league(session, number)
            )
        if found is None:
            raise NotFoundError(target.kind.value, target.entity_id)
        return number, found[1].name, found[1].sport

    async def list_watchlist(self, user_id: int) -> tuple[WatchItem, ...]:
        """See :meth:`WatchlistService.list_watchlist`."""
        async with self._core.ctx.sessions() as session:
            records = await data.list_watchlist(session, user_id)
            items = []
            for record in records:
                found = (
                    await data.get_team(session, record.entity_id)
                    if record.kind is data.WatchKind.TEAM
                    else await data.get_league(session, record.entity_id)
                )
                if found is not None:
                    items.append(_item(record, found[1].name))
        return tuple(sorted(items, key=lambda item: (item.name, item.target.kind)))

    async def add_watch(self, user_id: int, target: WatchTarget) -> WatchItem:
        """See :meth:`WatchlistService.add_watch`."""
        number, name, sport = await self._entity(target)
        async with self._core.ctx.sessions.begin() as session:
            await data.ensure_user(session, user_id, timezone=(await self._tz(user_id)))
            record = await data.add_watch(
                session,
                user_id,
                kind=data.WatchKind(target.kind.value),
                sport=sport,
                entity_id=number,
            )
        return _item(record, name)

    async def _tz(self, user_id: int) -> str:
        return (await self._core.profile(user_id)).zone.key

    async def remove_watch(self, user_id: int, target: WatchTarget) -> bool:
        """See :meth:`WatchlistService.remove_watch`."""
        number = parse_id(target.entity_id, "entity_id")
        async with self._core.ctx.sessions.begin() as session:
            return await data.remove_watch(
                session, user_id, kind=data.WatchKind(target.kind.value), entity_id=number
            )

    async def set_notifications(
        self, user_id: int, target: WatchTarget, notifications: frozenset[WatchNotification]
    ) -> WatchItem:
        """See :meth:`WatchlistService.set_notifications`."""
        number, name, _ = await self._entity(target)
        flags = tuple(sorted(flag.value for flag in notifications))
        async with self._core.ctx.sessions.begin() as session:
            record = await data.set_watch_notifications(
                session,
                user_id,
                kind=data.WatchKind(target.kind.value),
                entity_id=number,
                notify_flags=flags,
            )
        if record is None:
            raise NotFoundError("watch", target.entity_id)
        return _item(record, name)


def _item(record: data.WatchRecord, name: str) -> WatchItem:
    return WatchItem(
        target=WatchTarget(WatchKind(record.kind.value), str(record.entity_id)),
        name=name,
        sport=record.sport,
        notifications=frozenset(WatchNotification(flag) for flag in record.notify_flags),
    )


def _llm_quota(reviewer: CandidateReviewer) -> LlmQuota | None:
    usage = llm_usage(reviewer)
    if usage is None:
        return None
    return LlmQuota(
        requests_used=usage.requests_used,
        requests_limit=usage.requests_limit,
        tokens_used=usage.tokens_used,
        tokens_limit=usage.tokens_limit,
    )


class RealStatusService:
    """:class:`~app.services.interfaces.SystemStatusService` for ``/holat``."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    async def system_status(self) -> SystemStatus:
        """See :meth:`SystemStatusService.system_status`."""
        ctx = self._core.ctx
        quotas = [
            ApiQuota(
                provider=snap.provider,
                sport=snap.sport,
                used=snap.used,
                limit=snap.limit,
                reserved_manual=snap.manual_used,
                manual_reserve=snap.manual_reserve,
                manual_used=snap.manual_used,
            )
            for snap in [await provider.quota() for provider in ctx.providers.values()]
        ]
        if not quotas:
            quotas = await self._stored_quotas()
        runs = await ctx.ledger.last_runs()
        jobs = tuple(
            JobStatus(
                name=name,
                last_run_at=entry.done_at,
                ok=entry.ok,
                detail=entry.detail,
            )
            for name, entry in sorted(runs.items())
        )
        refresh = runs.get("refresh")
        learning = runs.get("learning")
        mode = mode_of(self._core)
        missed = (
            mode is not LearningMode.OFF
            and learning is not None
            and learning.done_at is not None
            and self._core.now() - learning.done_at > LEARNING_OVERDUE_AFTER
        )
        errors = tuple(
            ErrorEntry(at=at, component=component, message=scrub_text(message))
            for at, component, message in await ctx.ledger.recent_errors()
        )
        return SystemStatus(
            quotas=tuple(quotas),
            last_refresh_at=None if refresh is None else refresh.done_at,
            jobs=jobs,
            recent_errors=errors,
            llm=_llm_quota(ctx.reviewer),
            learning_mode=mode,
            learning_missed=missed,
        )

    async def _stored_quotas(self) -> list[ApiQuota]:
        """Usage rows of today when no provider is configured (limit = the free plan's)."""
        today = self._core.now().astimezone(UTC).date()
        async with self._core.ctx.sessions() as session:
            rows = await data.usage_on(session, today)
        quotas = [
            ApiQuota(
                provider=row.key.provider,
                sport=Sport.FOOTBALL if row.key.sport == "football" else None,
                used=row.counts.requests_used,
                limit=DEFAULT_DAILY_LIMIT,
                reserved_manual=row.counts.manual_used,
                manual_reserve=DEFAULT_MANUAL_RESERVE,
                manual_used=row.counts.manual_used,
            )
            for row in rows
        ]
        return quotas or [
            ApiQuota(
                provider="api-football",
                sport=Sport.FOOTBALL,
                used=0,
                limit=DEFAULT_DAILY_LIMIT,
                reserved_manual=0,
                manual_reserve=DEFAULT_MANUAL_RESERVE,
                manual_used=0,
            )
        ]
