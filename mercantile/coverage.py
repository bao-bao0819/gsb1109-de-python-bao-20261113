"""Offline collection directory for hierarchically published base maps.

A base map is published as a stack of XYZ tiles: a tile is only ever sent as a
"sub map" when the area it covers has changed. Readers render an area from the
deepest tile they have for it and fall back to a parent tile where no deeper one
is available.

Two kinds of files arrive in an offline collection directory:

* a *publication* (``action: "publish"``), which offers one or more tiles, and
* a *withdrawal* (``action: "retract"``), which removes an offer.

A withdrawal is ambiguous on its own. It can mean either

* ``"fallback"``: stop serving the withdrawn tile and fall back to its parent
  map, or
* ``"stop"``: stop providing the area altogether (a hole in the coverage).

Every file carries the *revision number* of the source that produced it so that
files which arrive late can be reconciled against files that already arrived.

Coverage is kept as a quadtree. Each node may carry an offered map (with the
revision that published it) and may be marked as a stopped hole. Independently
published sub-maps are nodes of their own and survive the withdrawal of a
coarser tile in ``fallback`` mode.

The :class:`OfflineCoverage` class keeps the authoritative state for one
collection directory. Files are folded in with :meth:`OfflineCoverage.apply`
(or :meth:`OfflineCoverage.apply_many`) and the tiles a reader should use are
returned by :meth:`OfflineCoverage.leaves`. Replaying the same list of files is
idempotent: the coverage a reader sees never drops another level.
"""

import json
import os

import mercantile


PUBLISH = "publish"
RETRACT = "retract"

FALLBACK = "fallback"
STOP = "stop"

_ACTIONS = (PUBLISH, RETRACT)
_RETRACT_MODES = (FALLBACK, STOP)


class CoverageError(mercantile.MercantileError):
    """Raised when an offline coverage file or record is invalid."""


class _Node(object):
    """A single quadtree node (one XYZ tile) in the coverage tree."""

    __slots__ = ("children", "offered_rev", "stopped")

    def __init__(self):
        self.children = None
        self.offered_rev = None
        self.stopped = False

    def is_empty(self):
        return self.offered_rev is None and not self.stopped and not self.children


def _as_tile(value):
    """Coerce a tile description into a :class:`mercantile.Tile`."""
    if isinstance(value, mercantile.Tile):
        return value
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return mercantile.Tile(int(value[0]), int(value[1]), int(value[2]))
    raise CoverageError("invalid tile: %r" % (value,))


def _record_rev(record):
    try:
        return int(record["rev"])
    except (KeyError, TypeError, ValueError):
        raise CoverageError("record is missing an integer 'rev': %r" % (record,))


def _record_action(record):
    action = record.get("action", PUBLISH)
    if action not in _ACTIONS:
        raise CoverageError("unknown action %r" % (action,))
    return action


def _record_tiles(record):
    raw = record.get("tiles")
    if raw is None and "tile" in record:
        raw = [record["tile"]]
    if not raw:
        raise CoverageError("record has no tiles: %r" % (record,))
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raw = [raw]
    return [_as_tile(item) for item in raw]


def _record_retract_mode(record):
    mode = record.get("retract_mode", FALLBACK)
    if mode not in _RETRACT_MODES:
        raise CoverageError("unknown retract_mode %r" % (mode,))
    return mode


def _quadrant_index(parent, child):
    """Row-major quadrant index of ``child`` within ``parent``."""
    dx = child.x - parent.x * 2
    dy = child.y - parent.y * 2
    return dy * 2 + dx


def _child_tile(parent, index):
    return mercantile.Tile(
        parent.x * 2 + (index % 2),
        parent.y * 2 + (index // 2),
        parent.z + 1,
    )


class OfflineCoverage(object):
    """Authoritative coverage state for one offline collection directory.

    Parameters
    ----------
    path : str, optional
        Directory holding collection files. When given, files are read from
        ``path``; otherwise the object is purely in memory.
    """

    def __init__(self, path=None):
        self.path = path
        self.root = _Node()
        # Highest revision already folded in for each source.
        self._source_rev = {}

    @property
    def revisions(self):
        """Mapping of source identifier to the highest revision applied."""
        return dict(self._source_rev)

    def _source_of(self, record):
        return record.get("source", "default")

    def _path_to(self, tile):
        """Return the root-to-tile chain of tiles, inclusive of ``tile``."""
        chain = []
        current = tile
        while current.z > 0:
            chain.append(current)
            current = mercantile.parent(current)
        chain.append(mercantile.Tile(0, 0, 0))
        chain.reverse()
        return chain

    def _navigate(self, tile):
        """Return the node for ``tile``, creating intermediate nodes."""
        node = self.root
        chain = self._path_to(tile)
        for parent, child in zip(chain, chain[1:]):
            if node.children is None:
                node.children = {}
            index = _quadrant_index(parent, child)
            node = node.children.setdefault(index, _Node())
        return node, chain

    def _prune(self, chain):
        """Remove empty nodes bottom-up, leaving the root in place."""
        node = self.root
        anchors = [self.root]
        for parent, child in zip(chain, chain[1:]):
            index = _quadrant_index(parent, child)
            node = node.children[index]
            anchors.append(node)
        for depth in range(len(anchors) - 1, 0, -1):
            child_node = anchors[depth]
            parent_node = anchors[depth - 1]
            if child_node.is_empty():
                index = _quadrant_index(chain[depth - 1], chain[depth])
                del parent_node.children[index]
                if not parent_node.children:
                    parent_node.children = None

    def apply(self, record):
        """Fold a single collection file (record) into the coverage.

        Records belonging to a source revision that has already been superseded
        are considered late and are ignored. Returns the record when it was
        applied or ``None`` when it was discarded as stale.
        """
        rev = _record_rev(record)
        source = self._source_of(record)
        last = self._source_rev.get(source)
        if last is not None and rev < last:
            return None
        self._source_rev[source] = rev

        action = _record_action(record)
        tiles = _record_tiles(record)
        if action == PUBLISH:
            for tile in tiles:
                self._publish(tile, rev)
        else:
            mode = _record_retract_mode(record)
            for tile in tiles:
                self._retract(tile, mode)
        return record

    def apply_many(self, records):
        """Apply records in source-revision order and report the impact.

        Records are first sorted by ``(source, rev)`` so files which arrive out
        of order are still reconciled deterministically. Returns a
        :class:`ChangeReport` describing the leaf ranges that actually changed
        relative to the coverage before the batch.
        """
        before_leaves = self.leaves()
        ordered = sorted(
            records, key=lambda r: (self._source_of(r), _record_rev(r))
        )
        applied = []
        stale = []
        for record in ordered:
            if self.apply(record) is None:
                stale.append(record)
            else:
                applied.append(record)
        after_leaves = self.leaves()
        # Compare both snapshots at one zoom so the diff reflects exact areas
        # rather than mixing tiles from different zoom levels.
        zooms = [t.z for t in before_leaves] + [t.z for t in after_leaves]
        common_zoom = max(zooms, default=0)
        before = self._expand(before_leaves, common_zoom)
        after = self._expand(after_leaves, common_zoom)
        return ChangeReport(
            added=sorted(after - before),
            removed=sorted(before - after),
            applied=applied,
            stale=stale,
        )

    def _publish(self, tile, rev):
        """Offer ``tile`` at ``rev`` and supersede every deeper tile."""
        node, chain = self._navigate(tile)
        node.offered_rev = rev
        node.stopped = False
        # Anything deeper is now superseded by this fresher map.
        node.children = None
        self._prune(chain)

    def _retract(self, tile, mode):
        """Withdraw ``tile`` without ever deepening the current coverage."""
        node, chain = self._navigate(tile)
        if mode == STOP:
            # The whole area goes dark, independent sub-maps included.
            node.stopped = True
            node.offered_rev = None
            node.children = None
        else:
            # Revert to a coarser map: drop this offer (and any prior stop
            # marker) while independently published sub-maps below survive.
            node.offered_rev = None
            node.stopped = False
            if not node.children:
                node.children = None
        self._prune(chain)

    def leaves(self):
        """Return the leaf tiles a reader should use, sorted by zoom.

        Coarser maps are expanded into sibling quadrants only where a deeper
        independent map exists, so the result is a disjoint antichain. Stopped
        areas are omitted and independently published sub-maps are preserved.
        """
        found = []
        self._collect(self.root, mercantile.Tile(0, 0, 0), None, found)
        return sorted(found, key=lambda t: (t.z, t.x, t.y))

    def _collect(self, node, tile, ancestor_offer, found):
        if node.stopped:
            # A stop blocks coarser coverage from flowing in, but explicit
            # offers below the marker still provide their own sub-areas.
            ancestor_offer = None
        if node.offered_rev is not None:
            ancestor_offer = tile
        if not node.children:
            if ancestor_offer is not None:
                found.append(ancestor_offer)
            return
        for index in range(4):
            child = node.children.get(index)
            child_tile = _child_tile(tile, index)
            if child is None:
                if ancestor_offer is not None:
                    found.append(child_tile)
            else:
                self._collect(child, child_tile, ancestor_offer, found)

    def _expand(self, leaves, zoom):
        """Expand leaf tiles to a single common zoom for area comparisons."""
        uniform = set()
        for tile in leaves:
            if tile.z == zoom:
                uniform.add(tile)
            else:
                uniform.update(mercantile.children(tile, zoom=zoom))
        return uniform

    def contains(self, tile):
        """Return ``True`` when the area of ``tile`` is still provided."""
        tile = _as_tile(tile)
        node = self.root
        covered = node.offered_rev is not None and not node.stopped
        chain = self._path_to(tile)
        for parent, child in zip(chain, chain[1:]):
            if not node.children:
                return covered
            node = node.children.get(_quadrant_index(parent, child))
            if node is None:
                return covered
            # A stop blocks inherited coarse coverage; an explicit offer below
            # it provides its own sub-area again.
            if node.stopped:
                covered = False
            if node.offered_rev is not None:
                covered = True
        return covered

    def load_directory(self):
        """Load and apply every ``*.json`` collection file in ``path``."""
        if not self.path:
            raise CoverageError("no directory configured")
        records = []
        for name in sorted(os.listdir(self.path)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(self.path, name)) as fh:
                data = json.load(fh)
            if isinstance(data, list):
                records.extend(data)
            else:
                records.append(data)
        return self.apply_many(records)


class ChangeReport(object):
    """The leaf-level result of applying a batch of collection files.

    Attributes
    ----------
    added, removed : list
        Leaf ranges present after the batch but not before (``added``) and vice
        versa (``removed``), compared at a single common zoom so the areas are
        exact.
    applied, stale : list
        Records that were folded in and records discarded as late.
    """

    def __init__(self, added=None, removed=None, applied=None, stale=None):
        self.added = added or []
        self.removed = removed or []
        self.applied = applied or []
        self.stale = stale or []

    @property
    def affected(self):
        """Every leaf range touched by the change, in a stable order."""
        return sorted(set(self.added) | set(self.removed))

    def as_dict(self):
        return {
            "added": [list(t) for t in self.added],
            "removed": [list(t) for t in self.removed],
            "affected": [list(t) for t in self.affected],
            "stale": len(self.stale),
            "applied": len(self.applied),
        }
