"""Tools for classifying weighted topic-search rankings.

Public API:

* ``feasible_polygon``  - exact convex set of admissible weights,
* ``classify``          - guaranteed / possible / impossible verdicts,
* ``rank_table`` / ``rank_of`` - concrete rankings under a shared weight vector,
* ``load_records``      - parse CSV or JSON exports,
* ``main``              - command line entry point.

All feasibility, tie and strict-inequality reasoning is exact (rational); only
values returned for display are converted to floats.
"""

from .analysis import (
    TOP_K,
    classify,
    rank_of,
    rank_table,
    weighted_score,
)
from .exact_engine import feasible_polygon
from .io_utils import load_records, parse_ranges

__all__ = [
    "TOP_K",
    "classify",
    "feasible_polygon",
    "load_records",
    "parse_ranges",
    "rank_of",
    "rank_table",
    "weighted_score",
]
