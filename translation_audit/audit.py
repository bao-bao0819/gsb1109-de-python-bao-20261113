"""Batch translation audit.

The supplier claims the new facility outline batch is the old batch shifted by
one common translation (dx, dy).  Some link IDs are missing in both batches,
and duplicate shapes exist, so an outline may have several look-alikes.

Inputs are JSON documents with this schema::

    {
      "tolerance": 0.12,                       # scalar or {"x": .., "y": ..}
      "old": [{"id": "F1", "geometry": "POLYGON((...))" | <geojson mapping>}],
      "new": [...],
      "known_pairs": [                         # optional explicit controls
        {"old_id": "F1", "new_id": "F1n", "tolerance": 0.05}
      ]
    }

Outlines whose ``id`` appears in both batches are treated as known control
correspondences automatically.  Tolerance is a per-vertex absolute bound:
matching vertex k of an old/new pair requires
``|(new_k - old_k) - T| <= tolerance`` component-wise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from itertools import product
from pathlib import Path
from typing import Any

from translation_audit.geometry import Box
from translation_audit.geometry import Vec
from translation_audit.geometry import all_vertices
from translation_audit.geometry import as_tolerance_xy
from translation_audit.geometry import box_contains
from translation_audit.geometry import box_half_extents
from translation_audit.geometry import intersect_boxes
from translation_audit.geometry import parse_geometry
from translation_audit.geometry import shape_signature
from translation_audit.geometry import translation_box


@dataclass(frozen=True)
class Outline:
    fid: str | None
    geometry: Any
    vertices: tuple[Vec, ...]
    signature: tuple[tuple[int, int], ...]
    tolerance: float | tuple[float, float] | None


@dataclass(frozen=True)
class Edge:
    """One feasible old->new correspondence with its displacement box."""

    old_id: str | None
    new_id: str | None
    old_index: int
    new_index: int
    box: Box
    vertex_count: int


@dataclass(frozen=True)
class ControlEvidence:
    label: str
    box: Box
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "box": list(self.box), "source": self.source}


@dataclass(frozen=True)
class Matching:
    edges: tuple[Edge, ...]
    box: Box

    def as_dict(self) -> dict[str, Any]:
        return {
            "box": list(self.box),
            "pairs": [
                {"old": edge.old_id, "new": edge.new_id} for edge in self.edges
            ],
        }


@dataclass
class AuditResult:
    common_translation_possible: bool
    allowed_translation_boxes: list[Box] = field(default_factory=list)
    unique_matches: dict[str, str] = field(default_factory=dict)
    ambiguous_matches: dict[str, list[str]] = field(default_factory=dict)
    unmatched: dict[str, list[str]] = field(default_factory=dict)
    feasible_matchings: list[Matching] = field(default_factory=list)
    controls: list[ControlEvidence] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "common_translation_possible": self.common_translation_possible,
            "allowed_translation_boxes": [
                list(box) for box in self.allowed_translation_boxes
            ],
            "unique_matches": self.unique_matches,
            "ambiguous_matches": self.ambiguous_matches,
            "unmatched": self.unmatched,
            "feasible_matchings": [
                matching.as_dict() for matching in self.feasible_matchings
            ],
            "controls": [control.as_dict() for control in self.controls],
            "conflicts": self.conflicts,
        }


def load_dataset(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _build_outlines(
    records: list[dict[str, Any]],
    default_tolerance: float | tuple[float, float] | None,
    prefix: str,
) -> list[Outline]:
    outlines: list[Outline] = []
    for index, record in enumerate(records):
        geometry = parse_geometry(record["geometry"])
        vertices = tuple(all_vertices(geometry))
        outlines.append(
            Outline(
                fid=record.get("id", f"{prefix}#{index}"),
                geometry=geometry,
                vertices=vertices,
                signature=shape_signature(geometry),
                tolerance=record.get("tolerance", default_tolerance),
            )
        )
    return outlines


def _edge_box(old: Outline, new: Outline) -> Box | None:
    """Feasible common-translation box for one outline pair.

    Vertices are paired in coordinate-sorted order so ring orientation and
    start vertex do not matter.  Returns None when vertex counts differ or the
    per-vertex boxes have an empty intersection.
    """
    if len(old.vertices) != len(new.vertices):
        return None
    tolerance = new.tolerance if new.tolerance is not None else old.tolerance
    if tolerance is None:
        msg = "No error bound given for vertex matching"
        raise ValueError(msg)
    old_sorted = sorted(old.vertices)
    new_sorted = sorted(new.vertices)
    boxes = [
        translation_box(old_vertex, new_vertex, tolerance)
        for old_vertex, new_vertex in zip(old_sorted, new_sorted, strict=True)
    ]
    return intersect_boxes(boxes)


def _perfect_assignments(
    old_group: list[Outline],
    new_group: list[Outline],
    edges: dict[int, list[Edge]],
) -> list[tuple[Edge, ...]]:
    """All one-to-one assignments ignoring whether boxes share a translation."""
    if len(old_group) != len(new_group):
        return []
    assignments: list[tuple[Edge, ...]] = []
    old_indices = sorted(range(len(old_group)), key=lambda i: len(edges.get(i, ())))

    def search(pos: int, used_new: set[int], chosen: list[Edge]) -> None:
        if pos == len(old_indices):
            assignments.append(tuple(chosen))
            return
        old_index = old_indices[pos]
        for edge in edges.get(old_index, ()):
            if edge.new_index in used_new:
                continue
            chosen.append(edge)
            search(pos + 1, used_new | {edge.new_index}, chosen)
            chosen.pop()

    search(0, set(), [])
    return assignments


def _enumerate_matchings(
    old_group: list[Outline],
    new_group: list[Outline],
    edges: dict[int, list[Edge]],
) -> tuple[list[tuple[Edge, ...]], list[Matching]]:
    """Return one-to-one assignments and those supporting a common translation."""
    assignments = _perfect_assignments(old_group, new_group, edges)
    matchings: list[Matching] = []
    for chosen in assignments:
        box = intersect_boxes(edge.box for edge in chosen)
        if box is not None:
            matchings.append(Matching(edges=chosen, box=box))
    return assignments, matchings


def _merge_boxes_union(boxes: list[Box]) -> list[Box]:
    """Merge axis-aligned boxes that overlap; keeps disjoint boxes separate."""
    remaining = list(boxes)
    merged: list[Box] = []
    while remaining:
        base = remaining.pop()
        absorbed = True
        while absorbed:
            absorbed = False
            for candidate in list(remaining):
                if _boxes_overlap(base, candidate):
                    base = (
                        min(base[0], candidate[0]),
                        min(base[1], candidate[1]),
                        max(base[2], candidate[2]),
                        max(base[3], candidate[3]),
                    )
                    remaining.remove(candidate)
                    absorbed = True
        merged.append(base)
    return merged


def _boxes_overlap(first: Box, second: Box) -> bool:
    return not (
        first[2] < second[0]
        or second[2] < first[0]
        or first[3] < second[1]
        or second[3] < first[1]
    )


def _minimal_unsat_core(controls: list[ControlEvidence]) -> list[ControlEvidence]:
    """Smallest-prefix style minimal subset whose boxes have empty intersection."""
    core: list[ControlEvidence] = []
    current: Box | None = None
    for control in controls:
        core.append(control)
        current = (
            control.box
            if current is None
            else intersect_boxes([current, control.box])
        )
        if current is None:
            break
    # Trim controls that are not needed for the contradiction.
    refined = list(core)
    for control in list(refined):
        trial = [item for item in refined if item is not control]
        if trial and intersect_boxes(item.box for item in trial) is None:
            refined.remove(control)
    return refined


def audit_batch(dataset: dict[str, Any]) -> AuditResult:
    default_tolerance = dataset.get("tolerance")
    old_outlines = _build_outlines(dataset["old"], default_tolerance, "old")
    new_outlines = _build_outlines(dataset["new"], default_tolerance, "new")

    result = AuditResult(common_translation_possible=False)
    known_box, forced_pairs = _collect_known_controls(
        dataset, old_outlines, new_outlines, result
    )

    old_groups = _group_by_signature(old_outlines)
    new_groups = _group_by_signature(new_outlines)

    group_assignments: list[list[tuple[Edge, ...]]] = []
    group_matchings: list[list[Matching]] = []
    for signature, old_group in old_groups.items():
        new_group = new_groups.get(signature, [])
        edges: dict[int, list[Edge]] = {}
        forced_in_group = {
            old_index: new_index
            for old_index, old in enumerate(old_group)
            for new_index, new in enumerate(new_group)
            if (old.fid, new.fid) in forced_pairs
        }
        forced_new = set(forced_in_group.values())
        for old_index, old in enumerate(old_group):
            for new_index, new in enumerate(new_group):
                if (
                    old_index in forced_in_group
                    and new_index != forced_in_group[old_index]
                ):
                    continue
                if old_index not in forced_in_group and new_index in forced_new:
                    continue
                box = _edge_box(old, new)
                if box is not None:
                    edges.setdefault(old_index, []).append(
                        Edge(
                            old_id=old.fid,
                            new_id=new.fid,
                            old_index=old_index,
                            new_index=new_index,
                            box=box,
                            vertex_count=len(old.vertices),
                        )
                    )
        assignments, matchings = _enumerate_matchings(old_group, new_group, edges)
        group_assignments.append(assignments)
        group_matchings.append(matchings)
        _record_group_status(old_group, new_group, edges, assignments, result)

    if any(not assignments for assignments in group_assignments):
        _attach_conflicts(result, known_box)
        return result

    for signature, new_group in new_groups.items():
        if signature not in old_groups:
            for new in new_group:
                result.unmatched.setdefault(new.fid, []).append(
                    "new outline has no same-shape old counterpart"
                )

    if not group_assignments:
        result.common_translation_possible = True
        return result

    combo_boxes: list[Box] = []
    all_matchings: list[Matching] = []
    for combo in product(*group_assignments):
        edges = tuple(edge for assignment in combo for edge in assignment)
        boxes = [edge.box for edge in edges]
        if known_box is not None:
            boxes.append(known_box)
        box = intersect_boxes(boxes)
        if box is not None:
            combo_boxes.append(box)
            all_matchings.append(Matching(edges=edges, box=box))

    if not combo_boxes:
        result.feasible_matchings = [
            matching for group in group_matchings for matching in group
        ]
        _attach_conflicts(result, known_box)
        return result

    result.common_translation_possible = True
    result.allowed_translation_boxes = _merge_boxes_union(combo_boxes)
    result.feasible_matchings = all_matchings
    result.unique_matches = {}
    result.ambiguous_matches = {}
    _classify_matches(all_matchings, result)
    return result


def _collect_known_controls(
    dataset: dict[str, Any],
    old_outlines: list[Outline],
    new_outlines: list[Outline],
    result: AuditResult,
) -> tuple[Box | None, set[tuple[str, str]]]:
    old_by_id = {
        outline.fid: outline
        for outline in old_outlines
        if outline.fid is not None
    }
    new_by_id = {
        outline.fid: outline
        for outline in new_outlines
        if outline.fid is not None
    }
    explicit = dataset.get("known_pairs", [])
    boxes: list[Box] = []
    forced_pairs: set[tuple[str, str]] = set()

    shared_ids = sorted(set(old_by_id) & set(new_by_id))
    for fid in shared_ids:
        box = _edge_box(old_by_id[fid], new_by_id[fid])
        label = f"id={fid}"
        if box is None:
            result.conflicts.append(
                {
                    "type": "infeasible_control",
                    "evidence": [label],
                    "detail": (
                        "same-id outlines cannot be one translation within tolerance"
                    ),
                }
            )
            continue
        evidence = ControlEvidence(label=label, box=box, source="shared_id")
        result.controls.append(evidence)
        boxes.append(box)
        forced_pairs.add((fid, fid))

    for pair in explicit:
        old = old_by_id[pair["old_id"]]
        new = new_by_id[pair["new_id"]]
        tolerance = pair.get(
            "tolerance",
            new.tolerance if new.tolerance is not None else old.tolerance,
        )
        if tolerance is None:
            msg = f"Known pair {pair} has no error bound"
            raise ValueError(msg)
        box = _edge_box_with_tolerance(old, new, tolerance)
        label = f"{pair['old_id']}->{pair['new_id']}"
        if box is None:
            result.conflicts.append(
                {"type": "infeasible_control", "evidence": [label],
                 "detail": "known control pair has no feasible translation"}
            )
            continue
        evidence = ControlEvidence(label=label, box=box, source="known_pairs")
        result.controls.append(evidence)
        boxes.append(box)
        forced_pairs.add((pair["old_id"], pair["new_id"]))

    known_box = intersect_boxes(boxes) if boxes else None
    return known_box, forced_pairs


def _edge_box_with_tolerance(
    old: Outline, new: Outline, tolerance: float | tuple[float, float]
) -> Box | None:
    if len(old.vertices) != len(new.vertices):
        return None
    old_sorted = sorted(old.vertices)
    new_sorted = sorted(new.vertices)
    return intersect_boxes(
        translation_box(old_vertex, new_vertex, tolerance)
        for old_vertex, new_vertex in zip(old_sorted, new_sorted, strict=True)
    )


def _group_by_signature(outlines: list[Outline]) -> dict[tuple, list[Outline]]:
    groups: dict[tuple, list[Outline]] = {}
    for outline in outlines:
        groups.setdefault(outline.signature, []).append(outline)
    return groups


def _record_group_status(
    old_group: list[Outline],
    new_group: list[Outline],
    edges: dict[int, list[Edge]],
    assignments: list[tuple[Edge, ...]],
    result: AuditResult,
) -> None:
    group_label = _group_label(old_group)
    if len(old_group) != len(new_group):
        result.unmatched[group_label] = [
            f"old={len(old_group)} outlines vs new={len(new_group)} same-shape outlines"
        ]
        return
    if assignments:
        options: dict[str, set[str]] = {}
        for chosen in assignments:
            for edge in chosen:
                options.setdefault(edge.old_id, set()).add(edge.new_id)
        for name, targets in options.items():
            if len(targets) == 1:
                result.unique_matches.setdefault(name, next(iter(targets)))
            else:
                result.ambiguous_matches[name] = sorted(targets)
        return
    for old_index, old in enumerate(old_group):
        options_for_one = edges.get(old_index, [])
        name = old.fid
        if not options_for_one:
            result.unmatched.setdefault(name, []).append(
                "no same-shape new outline fits within tolerance"
            )
        else:
            result.unmatched.setdefault(name, []).append(
                "candidates exist but duplicates cannot be assigned one-to-one"
            )


def _group_label(group: list[Outline]) -> str:
    ids = [outline.fid for outline in group if outline.fid]
    return ",".join(ids) if ids else f"shape@{group[0].signature[:1]}"


def _attach_conflicts(result: AuditResult, known_box: Box | None) -> None:
    if len(result.controls) >= 2 and known_box is None:
        core = _minimal_unsat_core(result.controls)
        result.conflicts.append(
            {
                "type": "control_box_mismatch",
                "evidence": [control.as_dict() for control in core],
                "detail": (
                    "displacement boxes of these control pairs have empty intersection"
                ),
            }
        )
    if not result.conflicts:
        result.conflicts.append(
            {
                "type": "no_common_translation",
                "evidence": [control.as_dict() for control in result.controls],
                "detail": (
                    "no perfect one-to-one matching supports a single translation"
                ),
            }
        )


def _classify_matches(matchings: list[Matching], result: AuditResult) -> None:
    options: dict[str, set[str]] = {}
    for matching in matchings:
        for edge in matching.edges:
            name = edge.old_id or f"index#{edge.old_index}"
            options.setdefault(name, set()).add(
                edge.new_id or f"index#{edge.new_index}"
            )
    for name, targets in sorted(options.items()):
        if len(targets) == 1:
            result.unique_matches[name] = next(iter(targets))
        else:
            result.ambiguous_matches[name] = sorted(targets)


def describe_box(box: Box) -> str:
    half_x, half_y = box_half_extents(box)
    center_x = (box[0] + box[2]) / 2.0
    center_y = (box[1] + box[3]) / 2.0
    return (
        f"dx in [{box[0]:.6g}, {box[2]:.6g}], "
        f"dy in [{box[1]:.6g}, {box[3]:.6g}] "
        f"(center ({center_x:.6g}, {center_y:.6g}), "
        f"half-widths {half_x:.6g}/{half_y:.6g})"
    )


def point_allowed(point: Vec, boxes: list[Box]) -> bool:
    return any(box_contains(box, point) for box in boxes)


def tolerance_xy(tolerance: float | tuple[float, float] | None) -> Vec | None:
    if tolerance is None:
        return None
    return as_tolerance_xy(tolerance)
