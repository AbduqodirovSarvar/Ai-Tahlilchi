"""Data aggregator: Football-Data.org matches joined with football-data.co.uk Bet365 odds."""

from app.sports.aggregator.csv_odds import OddsCsvLoader, OddsSource, ParsedOdds, parse_odds_csv
from app.sports.aggregator.matching import MatchingEngine, MatchLink
from app.sports.aggregator.models import CombinedMatchDto, LinkStatus, MatchOdds, OddsRow
from app.sports.aggregator.service import DataAggregatorService

__all__ = [
    "CombinedMatchDto",
    "DataAggregatorService",
    "LinkStatus",
    "MatchLink",
    "MatchOdds",
    "MatchingEngine",
    "OddsCsvLoader",
    "OddsRow",
    "OddsSource",
    "ParsedOdds",
    "parse_odds_csv",
]
