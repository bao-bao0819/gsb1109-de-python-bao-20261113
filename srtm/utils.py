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

import logging    as mod_logging
import math       as mod_math
import zipfile    as mod_zipfile
import pathlib    as mod_pathlib
import os         as mod_os
import os.path    as mod_path

from io import BytesIO as cStringIO # looks hacky but we are working with bytes
from typing import *

ONE_DEGREE = 1000. * 10000.8 / 90.

DEFAULT_TIMEOUT = 15

# Coordinates are in decimal degrees, values below this are treated as exact
# coincidence/collinearity:
GEOMETRY_EPSILON = 1e-12

# A point is a (latitude, longitude) pair:
Point = Tuple[float, float]
# A barrier is one or more polylines made of such points:
Polyline = List[Point]


def _cross(point_1: Point, point_2: Point, point_3: Point) -> float:
    """
    Cross product of the vectors point_1->point_2 and point_1->point_3.
    """
    return (point_2[0] - point_1[0]) * (point_3[1] - point_1[1]) - \
           (point_2[1] - point_1[1]) * (point_3[0] - point_1[0])


def point_on_segment(point: Point, segment_start: Point, segment_end: Point,
                     epsilon: float=GEOMETRY_EPSILON) -> bool:
    """
    True if point lies on the segment segment_start->segment_end.

    A point coinciding with a segment endpoint (or anywhere on the segment)
    is considered to be on it.
    """
    if abs(_cross(segment_start, segment_end, point)) > epsilon:
        return False
    return min(segment_start[0], segment_end[0]) - epsilon <= point[0] <= \
           max(segment_start[0], segment_end[0]) + epsilon and \
           min(segment_start[1], segment_end[1]) - epsilon <= point[1] <= \
           max(segment_start[1], segment_end[1]) + epsilon


def segments_cross_strictly(point_1: Point, point_2: Point,
                            point_3: Point, point_4: Point,
                            epsilon: float=GEOMETRY_EPSILON) -> bool:
    """
    True if segment 1->2 crosses segment 3->4 through both segments' interiors.

    Touching an endpoint is deliberately *not* a crossing: a zero-width barrier
    ends at its endpoint, so a path grazing exactly that point does not pass to
    the other side of the barrier. Collinear overlaps are handled separately.

    Orientations within epsilon of zero are treated as collinear so that
    floating-point round-off does not turn an endpoint touch into a crossing.
    """
    d1 = _cross(point_3, point_4, point_1)
    d2 = _cross(point_3, point_4, point_2)
    d3 = _cross(point_1, point_2, point_3)
    d4 = _cross(point_1, point_2, point_4)

    def opposite(value_1: float, value_2: float) -> bool:
        if abs(value_1) <= epsilon or abs(value_2) <= epsilon:
            return False
        return (value_1 > 0 > value_2) or (value_1 < 0 < value_2)

    return opposite(d1, d2) and opposite(d3, d4)


def collinear_segments_overlap(point_1: Point, point_2: Point,
                               point_3: Point, point_4: Point,
                               epsilon: float=GEOMETRY_EPSILON) -> bool:
    """
    True if the two segments are collinear and share a positive-length part.

    Merely meeting at a common endpoint returns False.
    """
    if abs(_cross(point_1, point_2, point_3)) > epsilon or \
       abs(_cross(point_1, point_2, point_4)) > epsilon:
        return False

    if abs(point_2[0] - point_1[0]) > epsilon:
        start_1, end_1 = point_1[0], point_2[0]
        start_2, end_2 = point_3[0], point_4[0]
    else:
        start_1, end_1 = point_1[1], point_2[1]
        start_2, end_2 = point_3[1], point_4[1]

    low_1, high_1 = min(start_1, end_1), max(start_1, end_1)
    low_2, high_2 = min(start_2, end_2), max(start_2, end_2)

    return min(high_1, high_2) - max(low_1, low_2) > epsilon


def segment_is_blocked(query_point: Point, support_point: Point,
                       barrier_start: Point, barrier_end: Point) -> bool:
    """
    True if the straight path query_point->support_point cannot cross the
    barrier segment barrier_start->barrier_end.

    The explicit rules are:
      * the support point lying on the barrier (including a barrier endpoint)
        blocks the path,
      * a proper transversal crossing of the barrier blocks the path,
      * a collinear overlap with the barrier blocks the path,
      * merely grazing a barrier endpoint does not block, and
      * when the query point itself sits on the barrier, paths departing from
        it are allowed unless one of the rules above applies (the barrier is
        not crossed when it is only touched at the departure point).
    """
    if point_on_segment(support_point, barrier_start, barrier_end):
        return True
    if segments_cross_strictly(query_point, support_point,
                               barrier_start, barrier_end):
        return True
    if collinear_segments_overlap(query_point, support_point,
                                  barrier_start, barrier_end):
        return True
    return False


def _as_polylines(geometry: Any) -> List[Polyline]:
    """
    Normalize a barrier/region argument into a list of polylines.

    Accepted inputs are a single polyline ([(lat, lon), ...]) or a sequence of
    polylines ([[(lat, lon), ...], ...]).
    """
    if not geometry:
        return []

    first = geometry[0]
    if first and isinstance(first[0], (int, float)):
        return [[(float(point[0]), float(point[1])) for point in geometry]]

    result: List[Polyline] = []
    for polyline in geometry:
        if polyline:
            result.append([(float(point[0]), float(point[1]))
                           for point in polyline])
    return result


def normalize_barriers(barriers: Any) -> List[Polyline]:
    """
    Normalize barrier lines into a list of (lat, lon) polylines.

    Barriers are expressed in global coordinates, so a single polyline may span
    several SRTM tiles and is applied identically to every tile query.
    """
    return _as_polylines(barriers)


def normalize_valid_regions(valid_regions: Any) -> List[Polyline]:
    """
    Normalize sampling valid regions into a list of closed (lat, lon) rings.

    A ring may be given open (its first and last point are joined implicitly).
    """
    return _as_polylines(valid_regions)


def path_blocked_by_barriers(query_point: Point, support_point: Point,
                             barriers: Sequence[Polyline]) -> Optional[Tuple[int, int]]:
    """
    Return the (barrier_index, segment_index) blocking the path, or None.
    """
    for barrier_index, barrier in enumerate(barriers):
        for segment_index in range(len(barrier) - 1):
            if segment_is_blocked(query_point, support_point,
                                  barrier[segment_index],
                                  barrier[segment_index + 1]):
                return barrier_index, segment_index
    return None


def point_in_ring(point: Point, ring: Sequence[Point],
                  epsilon: float=GEOMETRY_EPSILON) -> bool:
    """
    Ray-casting point-in-polygon test for a single closed ring.

    Points on the ring boundary (including its vertices) count as inside.
    """
    if len(ring) < 3:
        return False

    inside = False
    latitude, longitude = point
    for index in range(len(ring)):
        start = ring[index]
        end = ring[(index + 1) % len(ring)]
        if point_on_segment(point, start, end, epsilon):
            return True
        if (start[1] > longitude) != (end[1] > longitude):
            crossing_latitude = start[0] + \
                (longitude - start[1]) * (end[0] - start[0]) / (end[1] - start[1])
            if crossing_latitude > latitude:
                inside = not inside
    return inside


def point_in_valid_regions(point: Point, valid_regions: Sequence[Polyline]) -> bool:
    """
    True if the point belongs to at least one valid sampling region.

    With no regions configured every point is valid.
    """
    if not valid_regions:
        return True
    for ring in valid_regions:
        if point_in_ring(point, ring):
            return True
    return False


class Color(NamedTuple):
    red: int
    green: int
    blue: int
    alpha: int

def distance(latitude_1: float, longitude_1: float, latitude_2: float, longitude_2: float) -> float:
    """
    Distance between two points.
    """
    coef = mod_math.cos(latitude_1 / 180. * mod_math.pi)
    x = latitude_1 - latitude_2
    y = (longitude_1 - longitude_2) * coef

    return mod_math.sqrt(x * x + y * y) * ONE_DEGREE

def get_color_between(color1: Color, color2: Color, i: float) -> Color:
    """ i is a number between 0 and 1, if 0 then color1, if 1 color2, ... """
    if i <= 0:
        return color1
    if i >= 1:
        return color2
    return Color(int(color1[0] + (color2[0] - color1[0]) * i),
            int(color1[1] + (color2[1] - color1[1]) * i),
            int(color1[2] + (color2[2] - color1[2]) * i),
            int(color1[3] + (color2[3] - color1[3]) * i))

def zip(contents: bytes, file_name: str) -> bytes:
    mod_logging.debug('Zipping %s bytes' % len(contents))
    result = cStringIO()
    zip_file = mod_zipfile.ZipFile(result, 'w', mod_zipfile.ZIP_DEFLATED, False)
    zip_file.writestr(file_name, contents)
    zip_file.close()
    result.seek(0)
    mod_logging.debug('Zipped')
    return result.read()

def unzip(contents: bytes) -> bytes:
    mod_logging.debug('Unzipping %s bytes' % len(contents))
    zip_file = mod_zipfile.ZipFile(cStringIO(contents))
    zip_info_list = zip_file.infolist()
    for zi in zip_info_list:
        if zi.filename[0] != ".":
            result = zip_file.open(zi).read()
            mod_logging.debug('Unzipped')
            return result
    raise Exception(f"No valid file found in {zip_info_list}")

class FileHandler:
    """
    The default file handler. It can be changed if you need to save/read SRTM
    files in a database or Amazon S3.
    """

    def __init__(self, local_cache_dir: Optional[str]=None) -> None:
        if local_cache_dir:
            self.local_cache_dir = local_cache_dir
        else:
            home_dir = str(mod_pathlib.Path.home()) or mod_os.environ.get("HOME") or mod_os.environ.get("HOMEPATH") or ""
            if not home_dir:
                raise Exception('No default HOME directory found')
            self.local_cache_dir = mod_os.sep.join([home_dir, '.cache', 'srtm'])

        if not mod_path.exists(self.local_cache_dir):
            print(f"Creating {self.local_cache_dir}")
            try:
                mod_os.makedirs(self.local_cache_dir)
            except Exception as e:
                print(f"Local cache dir: {self.local_cache_dir}")
                raise Exception(f"Error creating directory {self.local_cache_dir}: {e}")

    def exists(self, file_name: str) -> bool:
        return mod_path.exists(mod_os.path.join(self.local_cache_dir, file_name))

    def write(self, file_name: str, contents: bytes) -> None:
        print(4, len(contents))
        fn = mod_os.path.join(self.local_cache_dir, file_name)
        with open(fn, 'wb') as f:
            n = f.write(contents)
            mod_logging.debug(f"saved {n} bytes in {fn}")

    def read(self, file_name: str) -> bytes:
        with open(mod_os.path.join(self.local_cache_dir, file_name), 'rb') as f:
            return f.read()