"""Pipeline-backed implementation of every service protocol (``APP_SERVICES=real``).

Never imports the in-memory fake (see the import-linter contract). ``create_real_services``
returns the bot's :class:`~app.services.interfaces.Services` bundle plus the
:class:`~app.pipeline.delivery.Delivery` the scheduled jobs use to reach users.
"""

from dataclasses import dataclass

from app.pipeline.context import PipelineContext
from app.pipeline.delivery import Delivery
from app.services.interfaces import Notifier, Services
from app.services.real.account import (
    RealCatalogService,
    RealSettingsService,
    RealStatusService,
    RealTemplateService,
    RealWatchlistService,
)
from app.services.real.analysis import RealAnalysisService
from app.services.real.browser import RealDataBrowserService
from app.services.real.calls import RealCallLogService
from app.services.real.core import Core
from app.services.real.learning import RealLearningService
from app.services.real.notifier import LogOnlyNotifier, NotifierDelivery
from app.services.real.signals import (
    RealBetService,
    RealExpressService,
    RealReportService,
    RealResultService,
    RealSignalService,
)


@dataclass(frozen=True, slots=True)
class RealSystem:
    """The bot's services and the delivery channel of the scheduled jobs."""

    services: Services
    delivery: Delivery


def create_real_services(
    ctx: PipelineContext,
    notifier: Notifier | None = None,
    *,
    build: str = "",
    admin_ids: frozenset[int] | None = None,
) -> RealSystem:
    """Wire every real service over ``ctx``; without a ``notifier`` messages are only logged.

    ``build`` is the label of the running build, shown by ``/holat``. ``admin_ids`` limits who
    may refresh the database; ``None`` trusts the bot's whitelist (which is the admin list).
    """
    core = Core(ctx, admin_ids=admin_ids)
    services = Services(
        analysis=RealAnalysisService(core),
        signals=RealSignalService(core),
        catalog=RealCatalogService(core),
        settings=RealSettingsService(core),
        templates=RealTemplateService(core),
        watchlist=RealWatchlistService(core),
        bets=RealBetService(core),
        results=RealResultService(core),
        reports=RealReportService(core),
        status=RealStatusService(core, build=build),
        express=RealExpressService(core),
        learning=RealLearningService(core),
        browser=RealDataBrowserService(core),
        calls=RealCallLogService(core),
    )
    return RealSystem(services, NotifierDelivery(core, notifier or LogOnlyNotifier()))
