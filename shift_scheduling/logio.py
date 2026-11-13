"""
Minimal loader for the sparse production-log CSV format.

Expected columns (header row required)::

    job_id,start,end,station_type,pinned_station

The ``pinned_station`` column may be left blank; every other column is
mandatory. Timestamps are parsed as integers (minutes since shift start).
"""
from __future__ import annotations

import csv
from typing import Iterable, List, Mapping, Sequence, Tuple

from .reconstruction import JobLog


def parse_jobs(rows: Iterable[Mapping[str, str]]) -> List[JobLog]:
    jobs: List[JobLog] = []
    for row in rows:
        pinned = (row.get("pinned_station") or "").strip()
        jobs.append(
            JobLog(
                job_id=row["job_id"].strip(),
                start=int(row["start"]),
                end=int(row["end"]),
                station_type=row["station_type"].strip(),
                pinned_station=pinned or None,
            )
        )
    return jobs


def load_jobs(path: str) -> List[JobLog]:
    with open(path, newline="", encoding="utf-8") as handle:
        return parse_jobs(csv.DictReader(handle))


def load_station_roster(path: str) -> Mapping[str, Tuple[str, ...]]:
    """
    Load a two-column ``station_type,station`` roster, one workstation per
    row, grouping stations of the same type in file order.
    """
    grouped: dict = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            grouped.setdefault(row["station_type"].strip(), []).append(
                row["station"].strip()
            )
    return {station_type: tuple(stations) for station_type, stations in grouped.items()}
