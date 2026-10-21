"""Pure statistical models: probabilities, de-vig, EV and staking (no I/O).

This module is the public model API used by the pipeline (contract 6 in docs/contracts.md).
Functions are synchronous and CPU-bound; async callers run them via ``asyncio.to_thread``.
"""

from app.models.devig import (
    DevigMethod,
    devig,
    devig_power,
    devig_proportional,
    devig_shin,
    implied_probabilities,
    overround,
    shin_insider_share,
)
from app.models.exceptions import (
    ModelFitError,
    ModelInputError,
    UnknownTeamError,
)

__all__ = [
    "DevigMethod",
    "ModelFitError",
    "ModelInputError",
    "UnknownTeamError",
    "devig",
    "devig_power",
    "devig_proportional",
    "devig_shin",
    "implied_probabilities",
    "overround",
    "shin_insider_share",
]
