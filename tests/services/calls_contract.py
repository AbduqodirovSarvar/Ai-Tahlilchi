"""Reusable behavioural tests of the provider call log (contract v1.12, ``CallLogService``).

Runs against both the fake and the real services. The backing log must hold the sample set
of :func:`app.services._fake_calls.sample_call_records` (the fake lays it out itself, the real
module's tests insert it), so the assertions can rely on its shape: both providers, every
outcome, a few calls older than a day.
"""

from datetime import timedelta

import pytest

from app.domain import CallOutcome, Provider
from app.services.errors import NotFoundError, ValidationFailedError
from app.services.interfaces import CallLogRequest, Services

PAGE = 5
UNKNOWN_IDS = (0, -1, 424_242)


def _pages(total: int) -> int:
    return -(-total // PAGE)


class CallLogContract:
    """Paged, filtered, newest-first call log; bounded statistics."""

    async def test_calls_are_paged_newest_first(self, services: Services, user_id: int) -> None:
        first = await services.calls.list_calls(user_id, CallLogRequest(page_size=PAGE))
        assert first.total > PAGE
        assert first.page_count == _pages(first.total)
        assert len(first.items) == PAGE
        second = await services.calls.list_calls(user_id, CallLogRequest(page=1, page_size=PAGE))
        everything = first.items + second.items
        assert [c.at for c in everything] == sorted((c.at for c in everything), reverse=True)
        assert [c.call_id for c in everything] == sorted(
            (c.call_id for c in everything), reverse=True
        )
        assert {c.call_id for c in first.items}.isdisjoint(c.call_id for c in second.items)
        assert all(c.at.utcoffset() == timedelta(0) for c in everything)

    async def test_a_page_past_the_end_is_empty_not_an_error(
        self, services: Services, user_id: int
    ) -> None:
        page = await services.calls.list_calls(user_id, CallLogRequest(page=10_000, page_size=PAGE))
        assert page.items == ()
        assert page.total > 0

    async def test_the_last_page_holds_the_remainder(
        self, services: Services, user_id: int
    ) -> None:
        total = (await services.calls.list_calls(user_id, CallLogRequest())).total
        last_index = _pages(total) - 1
        last = await services.calls.list_calls(
            user_id, CallLogRequest(page=last_index, page_size=PAGE)
        )
        assert len(last.items) == total - last_index * PAGE

    async def test_filters_keep_only_matching_calls_and_combine(
        self, services: Services, user_id: int
    ) -> None:
        everything = await services.calls.list_calls(user_id, CallLogRequest(page_size=20))
        by_provider = 0
        for provider in Provider:
            page = await services.calls.list_calls(
                user_id, CallLogRequest(provider=provider, page_size=20)
            )
            assert page.items
            assert {c.provider for c in page.items} == {provider}
            by_provider += page.total
        assert by_provider == everything.total
        errors = await services.calls.list_calls(
            user_id, CallLogRequest(outcome=CallOutcome.ERROR, page_size=20)
        )
        assert errors.items
        assert {c.outcome for c in errors.items} == {CallOutcome.ERROR}
        both = await services.calls.list_calls(
            user_id,
            CallLogRequest(provider=Provider.GEMINI, outcome=CallOutcome.ERROR, page_size=20),
        )
        assert {(c.provider, c.outcome) for c in both.items} == {
            (Provider.GEMINI, CallOutcome.ERROR)
        }
        assert 0 < both.total < errors.total

    async def test_a_filter_without_matches_gives_an_empty_page(
        self, services: Services, user_id: int
    ) -> None:
        page = await services.calls.list_calls(
            user_id,
            CallLogRequest(provider=Provider.GEMINI, outcome=CallOutcome.QUOTA_BLOCKED),
        )
        assert (page.items, page.total, page.page_count) == ((), 0, 0)

    async def test_the_detail_extends_the_listed_row(
        self, services: Services, user_id: int
    ) -> None:
        page = await services.calls.list_calls(user_id, CallLogRequest(page_size=20))
        assert page.items
        for entry in page.items:
            detail = await services.calls.get_call(user_id, entry.call_id)
            assert (detail.call_id, detail.at, detail.provider) == (
                entry.call_id,
                entry.at,
                entry.provider,
            )
            assert detail.operation == entry.operation
            assert detail.outcome == entry.outcome
            assert detail.http_status == entry.http_status
            assert detail.request.startswith(entry.request_preview)

    async def test_failures_carry_their_error_and_cache_hits_no_body(
        self, services: Services, user_id: int
    ) -> None:
        errors = await services.calls.list_calls(
            user_id, CallLogRequest(outcome=CallOutcome.ERROR, page_size=20)
        )
        assert errors.items
        for entry in errors.items:
            assert (await services.calls.get_call(user_id, entry.call_id)).error
        cached = await services.calls.list_calls(
            user_id, CallLogRequest(outcome=CallOutcome.CACHED, page_size=20)
        )
        assert cached.items
        for entry in cached.items:
            detail = await services.calls.get_call(user_id, entry.call_id)
            assert detail.response is None
            assert entry.quota_cost == 0

    @pytest.mark.parametrize("call_id", UNKNOWN_IDS)
    async def test_an_unknown_call_is_not_found(
        self, services: Services, user_id: int, call_id: int
    ) -> None:
        with pytest.raises(NotFoundError):
            await services.calls.get_call(user_id, call_id)

    async def test_stats_summarise_the_period_per_operation(
        self, services: Services, user_id: int
    ) -> None:
        total = (await services.calls.list_calls(user_id, CallLogRequest())).total
        day = await services.calls.call_stats(user_id)
        month = await services.calls.call_stats(user_id, days=30)
        assert sum(s.calls for s in month) == total
        assert 0 < sum(s.calls for s in day) < total
        keys = [(s.provider, s.operation) for s in month]
        assert keys == sorted(keys)
        assert len(set(keys)) == len(keys)
        for stat in month:
            assert stat.errors + stat.cached + stat.blocked <= stat.calls
            assert stat.quota_cost >= 0
            assert stat.avg_duration_ms is None or stat.avg_duration_ms >= 0
        assert sum(s.errors for s in month) > 0
        assert sum(s.blocked for s in month) > 0

    async def test_stats_leave_out_calls_older_than_the_period(
        self, services: Services, user_id: int
    ) -> None:
        week = sum(s.calls for s in await services.calls.call_stats(user_id, days=7))
        two_days = sum(s.calls for s in await services.calls.call_stats(user_id, days=2))
        assert 0 < two_days < week

    @pytest.mark.parametrize("days", [0, -1, 31, 1000])
    async def test_stats_period_is_bounded(
        self, services: Services, user_id: int, days: int
    ) -> None:
        with pytest.raises(ValidationFailedError):
            await services.calls.call_stats(user_id, days=days)

    @pytest.mark.parametrize("days", [1, 30])
    async def test_the_period_bounds_are_accepted(
        self, services: Services, user_id: int, days: int
    ) -> None:
        assert await services.calls.call_stats(user_id, days=days)

    @pytest.mark.parametrize(("page", "page_size"), [(-1, 5), (0, 0), (0, 21), (0, -3)])
    def test_an_invalid_request_cannot_be_built(self, page: int, page_size: int) -> None:
        with pytest.raises(ValidationFailedError):
            CallLogRequest(page=page, page_size=page_size)
