"""Migrate a legacy multi-level tile directory to the XYZ convention.

The old vendor's directory may follow TMS (Y axis flipped), may be
mislabeled by one zoom level, or both.  This tool evaluates the
batch-consistent candidate conventions (Y flip on/off, zoom shift
-1/0/+1) against the available ground-truth evidence:

* known parent/child pairs (a child depicts a sub-extent of its
  parent, optionally annotated with the corner of the parent it
  covers -- incomplete child sets still count as evidence), and
* landmarks annotated with the corner of the tile image they fall
  in; the same landmark seen in two tiles must sit in the
  intersection of the two annotated quadrants.

The convention consistent with all evidence (preferring the simplest)
is applied to the whole batch and the verifiable parent/child
relations are kept.  If no batch-consistent convention verifies every
relation, the earliest vendor level pair (parent z, child z) whose
relations fail is reported, together with whether flipping that
single child level resolves the contradiction.

Input (JSON, file or stdin)::

    {
      "tiles":     [{"id": "a", "z": 9, "x": 106, "y": 193}, ...],
      "relations": [{"parent": "a", "child": "b", "corner": "SE"}, ...],
      "landmarks": [{"tile": "a", "name": "lighthouse", "corner": "SE"}, ...]
    }

``relations`` (``corner`` optional) and ``landmarks`` are optional.
Output is a JSON report with the chosen convention, the converted XYZ
tiles, the verified and unverifiable relations, landmark consistency,
and any contradiction report.
"""

import json
import sys
from collections import defaultdict
from typing import NamedTuple

import mercantile

# XYZ child offsets (dx, dy) for each geographic corner of the parent.
CORNER_CHILD_OFFSETS = {
    "NW": (0, 0),
    "NE": (1, 0),
    "SW": (0, 1),
    "SE": (1, 1),
}


class Convention(NamedTuple):
    """A batch-consistent candidate convention for the legacy directory."""

    flip_y: bool = False  # True: directory Y follows TMS (flipped)
    dz: int = 0  # zoom levels mislabeled by this amount (vendor - XYZ)

    @property
    def name(self):
        base = "tms" if self.flip_y else "xyz"
        if self.dz:
            base += "%+dz" % self.dz
        return base


CANDIDATES = [
    Convention(flip_y=flip, dz=dz) for dz in (0, 1, -1) for flip in (False, True)
]


def convert_tile(tile, conv):
    """Convert one (z, x, y) tile to XYZ under ``conv``; None if invalid."""
    z, x, y = tile
    if z < 0 or x < 0 or y < 0:
        return None
    z2 = z + conv.dz
    if z2 < 0:
        return None
    y2 = (2**z2 - 1 - y) if conv.flip_y else y
    if not (0 <= x < 2**z2) or not (0 <= y2 < 2**z2):
        return None
    return (z2, x, y2)


def child_corner(parent, child):
    """Corner of ``parent`` occupied by ``child`` in XYZ, or None."""
    pz, px, py = parent
    cz, cx, cy = child
    if cz != pz + 1:
        return None
    offset = (cx - 2 * px, cy - 2 * py)
    for corner, candidate in CORNER_CHILD_OFFSETS.items():
        if offset == candidate:
            return corner
    return None


def is_descendant_or_same(parent, child):
    """True if XYZ ``child`` lies within (or equals) XYZ ``parent``."""
    pz, px, py = parent
    cz, cx, cy = child
    if cz < pz:
        return False
    while cz > pz:
        cx, cy = cx // 2, cy // 2
        cz -= 1
    return (cx, cy) == (px, py)


def quadrant_box(tile, corner):
    """Geographic bbox (w, s, e, n) of one corner quadrant of a tile."""
    z, x, y = tile
    bounds = mercantile.bounds(x, y, z)
    mid_lon = (bounds.west + bounds.east) / 2.0
    mid_lat = (bounds.south + bounds.north) / 2.0
    if corner == "NW":
        return (bounds.west, mid_lat, mid_lon, bounds.north)
    if corner == "NE":
        return (mid_lon, mid_lat, bounds.east, bounds.north)
    if corner == "SW":
        return (bounds.west, bounds.south, mid_lon, mid_lat)
    if corner == "SE":
        return (mid_lon, bounds.south, bounds.east, mid_lat)
    raise ValueError("unknown corner: %r" % (corner,))


def boxes_intersect(box_a, box_b, eps=1e-9):
    return (
        box_a[0] <= box_b[2] + eps
        and box_b[0] <= box_a[2] + eps
        and box_a[1] <= box_b[3] + eps
        and box_b[1] <= box_a[3] + eps
    )


def check_relation(relation, converted):
    """Verify one parent/child relation against converted XYZ tiles."""
    parent = converted[relation["parent"]]
    child = converted[relation["child"]]
    corner = relation.get("corner")
    if corner:
        return child_corner(parent, child) == corner
    return is_descendant_or_same(parent, child)


def landmark_score(landmarks, converted):
    """Count (ok, total) pairwise quadrant intersections per landmark."""
    by_name = defaultdict(list)
    for entry in landmarks:
        by_name[entry["name"]].append(entry)
    ok = total = 0
    for entries in by_name.values():
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                total += 1
                box_a = quadrant_box(converted[entries[i]["tile"]], entries[i]["corner"])
                box_b = quadrant_box(converted[entries[j]["tile"]], entries[j]["corner"])
                if boxes_intersect(box_a, box_b):
                    ok += 1
    return ok, total


def _convert_all(tiles, conv):
    converted = {}
    for tile_id, tile in tiles.items():
        xyz = convert_tile(tile, conv)
        if xyz is None:
            return None
        converted[tile_id] = xyz
    return converted


def _evaluate(tiles, relations, landmarks, conv):
    converted = _convert_all(tiles, conv)
    if converted is None:
        return None
    rel_ok = [check_relation(r, converted) for r in relations]
    lm_ok, lm_total = landmark_score(landmarks, converted)
    return {
        "convention": conv,
        "converted": converted,
        "rel_ok": rel_ok,
        "lm_ok": lm_ok,
        "lm_total": lm_total,
        "n_ok": sum(rel_ok) + lm_ok,
        "n_total": len(relations) + lm_total,
    }


def _try_level_flip(tiles, relations, landmarks, conv, level):
    """Re-check all evidence with vendor level ``level`` Y-flipped solo."""
    flipped = {
        tile_id: (z, x, (2**z - 1 - y) if z == level else y)
        for tile_id, (z, x, y) in tiles.items()
    }
    result = _evaluate(flipped, relations, landmarks, conv)
    return result is not None and result["n_ok"] == result["n_total"]


def analyze(data):
    """Analyze a legacy directory description and build the XYZ report."""
    tiles = {}
    for entry in data["tiles"]:
        tiles[entry["id"]] = (entry["z"], entry["x"], entry["y"])
    relations = data.get("relations", [])
    landmarks = data.get("landmarks", [])
    for rel in relations:
        for key in ("parent", "child"):
            if rel[key] not in tiles:
                raise ValueError("relation references unknown tile %r" % rel[key])
    for lm in landmarks:
        if lm["tile"] not in tiles:
            raise ValueError("landmark references unknown tile %r" % lm["tile"])

    results = []
    for conv in CANDIDATES:
        result = _evaluate(tiles, relations, landmarks, conv)
        if result is not None:
            results.append(result)
    if not results:
        raise ValueError("no candidate convention yields valid XYZ tiles")

    def rank(result):
        conv = result["convention"]
        return (result["n_ok"], -abs(conv.dz), conv.dz, not conv.flip_y)

    best = max(results, key=rank)
    conv = best["convention"]
    alternatives = sorted(
        r["convention"].name for r in results if r is not best and r["n_ok"] == best["n_ok"]
    )

    verified = [r for r, ok in zip(relations, best["rel_ok"]) if ok]
    unverified = [r for r, ok in zip(relations, best["rel_ok"]) if not ok]

    contradiction = None
    if unverified:
        def level_pair(rel):
            return (tiles[rel["parent"]][0], tiles[rel["child"]][0])

        earliest = min(level_pair(r) for r in unverified)
        child_level = earliest[1]
        contradiction = {
            "parent_level": earliest[0],
            "child_level": child_level,
            "relations": [r for r in unverified if level_pair(r) == earliest],
            "fixed_by_flipping_level": child_level
            if _try_level_flip(tiles, relations, landmarks, conv, child_level)
            else None,
        }

    return {
        "convention": {"name": conv.name, "flip_y": conv.flip_y, "dz": conv.dz},
        "alternatives": alternatives,
        "tiles": [
            {"id": tile_id, "z": z, "x": x, "y": y}
            for tile_id, (z, x, y) in sorted(best["converted"].items())
        ],
        "verified_relations": verified,
        "unverified_relations": unverified,
        "landmarks_consistent": best["lm_ok"] == best["lm_total"],
        "contradiction": contradiction,
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] != "-":
        with open(argv[0]) as fp:
            data = json.load(fp)
    else:
        data = json.load(sys.stdin)
    json.dump(analyze(data), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
