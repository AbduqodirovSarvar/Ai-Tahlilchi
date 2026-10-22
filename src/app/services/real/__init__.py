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
from app.services.real.core import Core
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


def create_real_services(ctx: PipelineContext, notifier: Notifier | None = None) -> RealSystem:
    """Wire every real service over ``ctx``; without a ``notifier`` messages are only logged."""
    core = Core(ctx)
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
        status=RealStatusService(core),
        express=RealExpressService(core),
    )
    return RealSystem(services, NotifierDelivery(core, notifier or LogOnlyNotifier()))
