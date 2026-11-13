"""Rolling deduplicated device counts up from child tiles to their parent.

Each tile reports the devices collected inside it. A device sitting on a
tile boundary may be reported by more than one child, so child totals can
not simply be added. Child tiles do not always provide the same quality of
evidence: some carry the actual device list, some only a total, some report
nothing yet, and some are confirmed to hold zero devices.

This module keeps those evidence states separate and derives the tightest
bounds on the parent's deduplicated device count that the available
evidence supports.

"""

from collections import namedtuple
from enum import Enum

from mercantile import Tile, children as child_tiles, parent as parent_tile


class EvidenceStatus(Enum):
    """The kind of evidence available for a tile."""

    OBSERVED = "observed"
    """The tile carries the explicit list of devices it collected."""

    COUNT_ONLY = "count_only"
    """Only a deduplicated total is known, without the device list."""

    MISSING = "missing"
    """No report has been received for the tile yet."""

    CONFIRMED_EMPTY = "confirmed_empty"
    """A report exists and confirms that the tile holds zero devices."""


# Evidence kinds that pin down a tile's device set exactly.
_EXACT_STATUSES = (EvidenceStatus.OBSERVED, EvidenceStatus.CONFIRMED_EMPTY)


class TileEvidence(
    namedtuple("TileEvidence", ["tile", "status", "devices", "count"])
):
    """Evidence about the devices collected in one tile.

    Attributes
    ----------
    tile : Tile
        The tile the evidence belongs to.
    status : EvidenceStatus
        Kind of evidence held for the tile.
    devices : frozenset or None
        Deduplicated device ids. Present for observed tiles, otherwise
        None.
    count : int or None
        Deduplicated device total. Present for observed and count-only
        tiles, otherwise None.

    """

    def __new__(cls, tile, status, devices=None, count=None):
        if not isinstance(status, EvidenceStatus):
            raise TypeError("status must be an EvidenceStatus value")
        tile = _as_tile(tile)

        if status is EvidenceStatus.OBSERVED:
            if devices is None:
                raise ValueError(
                    "observed tile evidence requires a device list"
                )
            device_set = frozenset(devices)
            if count is not None and count != len(device_set):
                raise ValueError(
                    "count does not match the number of listed devices"
                )
            count = len(device_set)
        elif status is EvidenceStatus.COUNT_ONLY:
            if devices is not None:
                raise ValueError(
                    "count-only tile evidence must not carry a device list"
                )
            count = _as_count(count)
            device_set = None
        else:
            if devices is not None or count is not None:
                raise ValueError(
                    "missing and confirmed-empty tile evidence carries no data"
                )
            device_set = None
            count = 0 if status is EvidenceStatus.CONFIRMED_EMPTY else None

        return super().__new__(cls, tile, status, device_set, count)

    @classmethod
    def observed(cls, tile, devices):
        """Evidence backed by the explicit list of collected devices."""
        return cls(tile, EvidenceStatus.OBSERVED, devices=frozenset(devices))

    @classmethod
    def count_only(cls, tile, count):
        """Evidence reduced to a deduplicated total."""
        return cls(tile, EvidenceStatus.COUNT_ONLY, count=_as_count(count))

    @classmethod
    def missing(cls, tile):
        """Placeholder for a tile that has not reported yet."""
        return cls(tile, EvidenceStatus.MISSING)

    @classmethod
    def empty(cls, tile):
        """Evidence confirming that the tile holds zero devices."""
        return cls(tile, EvidenceStatus.CONFIRMED_EMPTY)

    @property
    def is_exact(self):
        """True when the tile's device set is known exactly."""
        return self.status in _EXACT_STATUSES


class Bounds(namedtuple("Bounds", ["lower", "upper"])):
    """Lower and upper bounds on a deduplicated device count.

    Attributes
    ----------
    lower : int
        Smallest count consistent with the evidence.
    upper : int or None
        Largest count consistent with the evidence, or None when the
        evidence leaves the count unbounded.

    """

    @property
    def is_exact(self):
        """True when the bounds pin down a single value."""
        return self.upper is not None and self.lower == self.upper


class BoundTerm(
    namedtuple("BoundTerm", ["kind", "tile", "amount", "capped_by"])
):
    """One additive or extremal term behind a parent bound.

    Attributes
    ----------
    kind : str
        One of ``"known_union"``, ``"count_only"`` or ``"missing"``.
    tile : Tile or None
        Tile responsible for the term. None for the union of observed
        device lists.
    amount : int or None
        Contribution to the bound. None means the term is unbounded.
    capped_by : tuple of Tile
        Evidence-bearing tiles whose totals cap a missing tile's term.
        Empty for terms that do not need a cap.

    """

    __slots__ = ()

    def __new__(cls, kind, tile, amount, capped_by=()):
        return super().__new__(cls, kind, tile, amount, tuple(capped_by))


class Rollup(
    namedtuple(
        "Rollup",
        [
            "tile",
            "bounds",
            "known_devices",
            "exact",
            "lower_terms",
            "upper_terms",
            "children",
        ],
    )
):
    """Result of rolling child tile evidence up into one parent tile.

    Attributes
    ----------
    tile : Tile
        The parent tile.
    bounds : Bounds
        Bounds on the parent's deduplicated device count.
    known_devices : frozenset
        Devices proven to exist in the parent by observed child lists.
    exact : bool
        True when every child is known exactly, so that ``bounds`` is the
        exact deduplicated count.
    lower_terms : tuple of BoundTerm
        Evidence behind the lower bound.
    upper_terms : tuple of BoundTerm
        Additive evidence behind the upper bound. A missing tile's term
        carries the cap (or None) and the tiles that cap depends on.
    children : tuple of TileEvidence
        Evidence for all four child tiles, with unreported tiles filled
        in as missing.

    """

    __slots__ = ()

    def explain_lower(self):
        """Human-readable breakdown of the lower bound."""
        lines = []
        for term in self.lower_terms:
            if term.kind == "known_union":
                lines.append(
                    "union of observed device lists contributes %d"
                    % term.amount
                )
            elif term.kind == "count_only":
                lines.append(
                    "count-only tile %s with total %d may force the union "
                    "to this size" % (str(term.tile), term.amount)
                )
        if not lines:
            lines.append("no devices are proven to exist, lower bound is 0")
        return lines

    def explain_upper(self):
        """Human-readable breakdown of the upper bound, term by term."""
        lines = []
        for term in self.upper_terms:
            if term.kind == "known_union":
                lines.append(
                    "union of observed device lists contributes %d"
                    % term.amount
                )
            elif term.kind == "count_only":
                lines.append(
                    "count-only tile %s contributes at most its total %d"
                    % (str(term.tile), term.amount)
                )
            elif term.amount is None:
                lines.append(
                    "missing tile %s is unbounded: no sibling tile provides "
                    "positive evidence to cap it" % str(term.tile)
                )
            else:
                caps = ", ".join(str(t) for t in term.capped_by)
                lines.append(
                    "missing tile %s contributes at most %d, capped by the "
                    "smallest positive sibling total from %s"
                    % (term.tile, term.amount, caps)
                )
        if not lines:
            lines.append("no devices can exist, upper bound is 0")
        return lines


def _as_tile(tile):
    if isinstance(tile, Tile):
        return tile
    if isinstance(tile, (tuple, list)) and len(tile) == 3:
        return Tile(*tile)
    raise TypeError("tile must be a Tile or a (x, y, z) sequence")


def _as_count(count):
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("device count must be an integer")
    if count < 0:
        raise ValueError("device count must not be negative")
    return count


def _as_evidence(item):
    if isinstance(item, TileEvidence):
        return item
    if isinstance(item, dict):
        return TileEvidence(
            item["tile"],
            item["status"],
            devices=item.get("devices"),
            count=item.get("count"),
        )
    raise TypeError(
        "child evidence must be TileEvidence instances or equivalent dicts"
    )


def rollup(tile, child_evidence):
    """Roll child tile device evidence up into a parent tile.

    Parameters
    ----------
    tile : Tile or sequence of int, optional
        The parent tile. If None, the parent is derived from the first
        piece of child evidence.
    child_evidence : iterable of TileEvidence
        Evidence for child tiles one zoom level below the parent. Tiles
        without any entry are treated as missing. Duplicate tiles are an
        error.

    Returns
    -------
    Rollup

    Raises
    ------
    ValueError
        If a child does not belong to the given parent, or if the same
        child tile is reported twice.

    Notes
    -----
    When every child is known exactly (device list or confirmed empty),
    the result is the exact union, so boundary devices shared by several
    children are counted once. Count-only and missing children force
    bounds instead:

    * The lower bound is the largest of the observed union and any
      count-only total, since unknown device sets may overlap freely.
    * A count-only child adds at most its total to the upper bound.
    * A missing child adds at most the smallest positive total of its
      evidence-bearing siblings, because any boundary device it could
      hold must also be collectable by a sibling tile. With no positive
      sibling evidence the missing term is unbounded.
    * Confirmed-empty children contribute nothing on either side.

    """
    evidence_items = [_as_evidence(item) for item in child_evidence]

    if tile is None:
        if not evidence_items:
            raise ValueError(
                "the parent tile must be given when no child evidence "
                "is provided"
            )
        tile = parent_tile(evidence_items[0].tile)
    tile = _as_tile(tile)

    by_tile = {}
    for evidence in evidence_items:
        if parent_tile(evidence.tile) != tile:
            raise ValueError(
                "tile %s is not a child of parent %s" % (evidence.tile, tile)
            )
        if evidence.tile in by_tile:
            raise ValueError(
                "duplicate evidence for tile %s" % str(evidence.tile)
            )
        by_tile[evidence.tile] = evidence

    # Fill unreported slots with explicit missing evidence so that the
    # distinction between "not reported" and "confirmed empty" survives.
    filled_children = [
        by_tile.get(child, TileEvidence.missing(child))
        for child in child_tiles(tile)
    ]

    observed = [
        evidence
        for evidence in filled_children
        if evidence.status is EvidenceStatus.OBSERVED
    ]
    count_only = [
        evidence
        for evidence in filled_children
        if evidence.status is EvidenceStatus.COUNT_ONLY
    ]
    missing = [
        evidence
        for evidence in filled_children
        if evidence.status is EvidenceStatus.MISSING
    ]

    known_devices = frozenset()
    for evidence in observed:
        known_devices |= evidence.devices

    exact = not count_only and not missing
    if exact:
        lower = upper = len(known_devices)
        lower_terms = (BoundTerm("known_union", None, len(known_devices)),)
        upper_terms = lower_terms
        return Rollup(
            tile,
            Bounds(lower, upper),
            known_devices,
            True,
            lower_terms,
            upper_terms,
            tuple(filled_children),
        )

    # Lower bound: unknown sets may overlap each other and the observed
    # union completely, but a count-only total cannot fit inside a union
    # smaller than itself.
    largest_count_only = max(
        (evidence.count for evidence in count_only), default=0
    )
    lower = max(len(known_devices), largest_count_only)

    lower_terms = [BoundTerm("known_union", None, len(known_devices))]
    lower_terms.extend(
        BoundTerm("count_only", evidence.tile, evidence.count)
        for evidence in count_only
    )

    # Upper bound: assume every uncertain set is disjoint from the rest.
    upper_terms = [BoundTerm("known_union", None, len(known_devices))]
    upper_terms.extend(
        BoundTerm("count_only", evidence.tile, evidence.count)
        for evidence in count_only
    )

    positive_siblings = [
        evidence
        for evidence in (observed + count_only)
        if evidence.count > 0
    ]
    upper = len(known_devices) + sum(
        evidence.count for evidence in count_only
    )
    for evidence in missing:
        if positive_siblings:
            cap = min(item.count for item in positive_siblings)
            capped_by = tuple(item.tile for item in positive_siblings)
            upper += cap
            upper_terms.append(
                BoundTerm("missing", evidence.tile, cap, capped_by)
            )
        else:
            upper_terms.append(BoundTerm("missing", evidence.tile, None))

    if not positive_siblings and missing:
        upper = None

    return Rollup(
        tile,
        Bounds(lower, upper),
        known_devices,
        False,
        tuple(lower_terms),
        tuple(upper_terms),
        tuple(filled_children),
    )
