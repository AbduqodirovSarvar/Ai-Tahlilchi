"""Pure statistical models: probabilities, de-vig, EV and staking (no I/O).

This module is the public model API used by the pipeline (contract 6 in docs/contracts.md).
Functions are synchronous and CPU-bound; async callers run them via ``asyncio.to_thread``.
"""

from app.models.asian import (
    AsianOutcomeProbabilities,
    Settlement,
    is_quarter_line,
    settle,
    split_line,
)
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
from app.models.ev import (
    asian_expected_value,
    expected_value,
    fair_odds,
)
from app.models.exceptions import (
    ModelFitError,
    ModelInputError,
    UnknownTeamError,
)
from app.models.staking import (
    DEFAULT_KELLY_MULTIPLIER,
    DEFAULT_MAX_STAKE_FRACTION,
    asian_kelly_fraction,
    capped_fraction,
    flat_stake,
    kelly_fraction,
    stake_amount,
)

__all__ = [
    "DEFAULT_KELLY_MULTIPLIER",
    "DEFAULT_MAX_STAKE_FRACTION",
    "AsianOutcomeProbabilities",
    "DevigMethod",
    "ModelFitError",
    "ModelInputError",
    "Settlement",
    "UnknownTeamError",
    "asian_expected_value",
    "asian_kelly_fraction",
    "capped_fraction",
    "devig",
    "devig_power",
    "devig_proportional",
    "devig_shin",
    "expected_value",
    "fair_odds",
    "flat_stake",
    "implied_probabilities",
    "is_quarter_line",
    "kelly_fraction",
    "overround",
    "settle",
    "shin_insider_share",
    "split_line",
    "stake_amount",
]
