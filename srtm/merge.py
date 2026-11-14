# -*- coding: utf-8 -*-

# Copyright 2013 Tomo Krajina
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Merge adjacent HGT tiles into a single grid of points.

Neighbouring SRTM tiles share one row/column of samples along their common
integer-degree edge (and a single sample where four tiles meet). This module
aligns all samples on a global grid keyed by grid coordinate and resolution,
so that every real grid point occupies exactly one record, while every
contributing source file is kept for auditing.

Merge rules:

 * identical measured values are merged into a single value,
 * void samples never cancel a valid measurement,
 * different valid values at the same point are kept as a conflict, left
   for adjudication.

The result does not depend on the order in which files are added.
"""

from typing import *

from . import data as mod_data

# A sample is void when the tile reports no elevation for it:
_VOID = None


class GridPointRecord:
    """
    A single grid point of the merged grid, with all its sources.

    ``observations`` maps every distinct valid (non-void) value to the set
    of file names that reported it. ``void_sources`` holds the names of
    files that reported a void sample at this point.
    """

    __slots__ = ('steps_per_degree', 'latitude_index', 'longitude_index',
                 'observations', 'void_sources')

    def __init__(self, steps_per_degree: int, latitude_index: int, longitude_index: int) -> None:
        self.steps_per_degree = steps_per_degree
        self.latitude_index = latitude_index
        self.longitude_index = longitude_index
        self.observations: Dict[float, Set[str]] = {}
        self.void_sources: Set[str] = set()

    @property
    def latitude(self) -> float:
        return self.latitude_index / float(self.steps_per_degree)

    @property
    def longitude(self) -> float:
        return self.longitude_index / float(self.steps_per_degree)

    @property
    def resolution(self) -> float:
        return 1.0 / float(self.steps_per_degree)

    def add(self, value: Optional[float], source: str) -> None:
        if value is _VOID:
            self.void_sources.add(source)
        else:
            self.observations.setdefault(value, set()).add(source)

    @property
    def values(self) -> List[float]:
        """ All distinct valid values reported for this point, sorted. """
        return sorted(self.observations.keys())

    @property
    def is_conflict(self) -> bool:
        """ True when sources reported different valid values. """
        return len(self.observations) > 1

    @property
    def value(self) -> Optional[float]:
        """
        The merged value: the single valid value reported, or None when
        only void samples exist or when a conflict awaits adjudication.
        """
        if self.is_conflict or not self.observations:
            return None
        return next(iter(self.observations))

    @property
    def sources(self) -> Set[str]:
        """ All files that contributed a sample for this point. """
        result = set(self.void_sources)
        for sources in self.observations.values():
            result.update(sources)
        return result

    @property
    def evidence_count(self) -> int:
        """ Number of samples (valid or void) gathered for this point. """
        return len(self.void_sources) + sum(len(sources) for sources in self.observations.values())

    def __str__(self) -> str:
        return '[{0}:({1},{2}) value={3} evidence={4}]'.format(
            self.__class__.__name__, self.latitude, self.longitude,
            self.value, self.evidence_count)


class MergedElevationGrid:
    """
    A grid of deduplicated elevation points merged from several
    GeoElevationFile instances.
    """

    def __init__(self, files: Iterable[mod_data.GeoElevationFile]=()) -> None:
        # Keyed by (steps_per_degree, latitude_index, longitude_index):
        self._points: Dict[Tuple[int, int, int], GridPointRecord] = {}

        self.add_files(files)

    def add_files(self, files: Iterable[mod_data.GeoElevationFile]) -> None:
        for geo_file in files:
            self.add_file(geo_file)

    def add_file(self, geo_file: mod_data.GeoElevationFile) -> None:
        steps_per_degree = geo_file.square_side - 1

        # Grid indices of the file's upper-left (north-west) sample on the
        # global grid at this resolution. The file's starting latitude and
        # longitude are integer degrees, so these are exact:
        north_index = int(round((geo_file.latitude + 1) * steps_per_degree))
        west_index = int(round(geo_file.longitude * steps_per_degree))

        for row in range(geo_file.square_side):
            latitude_index = north_index - row
            for column in range(geo_file.square_side):
                longitude_index = west_index + column
                key = (steps_per_degree, latitude_index, longitude_index)
                record = self._points.get(key)
                if record is None:
                    record = GridPointRecord(*key)
                    self._points[key] = record
                value = geo_file.get_elevation_from_row_and_column(row, column)
                record.add(value, geo_file.file_name)

    def _key(self, latitude: float, longitude: float, resolution: Optional[float]=None) -> Tuple[int, int, int]:
        if resolution is None:
            resolutions = self.resolutions
            if len(resolutions) != 1:
                raise Exception('Resolution needed, grid holds: {0}'.format(sorted(resolutions)))
            resolution = resolutions.pop()
        steps_per_degree = int(round(1.0 / resolution))
        latitude_index = latitude * steps_per_degree
        longitude_index = longitude * steps_per_degree
        if abs(latitude_index - round(latitude_index)) > 1e-6 or \
           abs(longitude_index - round(longitude_index)) > 1e-6:
            raise ValueError('({0}, {1}) is not a grid point at resolution {2}'.format(
                             latitude, longitude, resolution))
        return steps_per_degree, int(round(latitude_index)), int(round(longitude_index))

    @property
    def resolutions(self) -> Set[float]:
        return set(1.0 / key[0] for key in self._points)

    def get_record(self, latitude: float, longitude: float, resolution: Optional[float]=None) -> Optional[GridPointRecord]:
        return self._points.get(self._key(latitude, longitude, resolution))

    def get_elevation(self, latitude: float, longitude: float, resolution: Optional[float]=None) -> Optional[float]:
        record = self.get_record(latitude, longitude, resolution)
        return record.value if record else None

    def records(self) -> List[GridPointRecord]:
        """ All records, one per real grid point, sorted by position. """
        return [self._points[key] for key in sorted(self._points)]

    def conflicts(self) -> List[GridPointRecord]:
        """ Points with disagreeing sources, left for adjudication. """
        return [record for record in self.records() if record.is_conflict]

    def __len__(self) -> int:
        return len(self._points)
