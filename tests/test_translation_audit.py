"""Tests for the translation audit tooling."""

from __future__ import annotations

import math

import pytest

from translation_audit.audit import audit_batch
from translation_audit.audit import load_dataset
from translation_audit.geometry import all_vertices
from translation_audit.geometry import intersect_boxes
from translation_audit.geometry import parse_geometry
from translation_audit.geometry import perfect_bipartite_match
from translation_audit.geometry import shape_signature
from translation_audit.geometry import translation_box


def test_translation_box_bounds() -> None:
    box = translation_box((0.0, 0.0), (2.0, 1.0), 0.1)
    assert box == pytest.approx((1.9, 0.9, 2.1, 1.1))


def test_intersect_boxes_detects_empty_intersection() -> None:
    first = translation_box((0, 0), (2, 1), 0.1)
    second = translation_box((0, 0), (5, -3), 0.1)
    assert intersect_boxes([first, second]) is None


def test_shape_signature_is_translation_invariant() -> None:
    old = parse_geometry("POLYGON((0 0, 4 0, 4 3, 0 3))")
    shifted = parse_geometry("POLYGON((2.05 1, 6.05 1, 6.05 4.05, 2.05 4.05))")
    triangle = parse_geometry("POLYGON((0 0, 1 0, 0 1))")
    assert shape_signature(old) == shape_signature(shifted)
    assert shape_signature(old) != shape_signature(triangle)


def test_all_vertices_keeps_count_for_polygon() -> None:
    polygon = parse_geometry("POLYGON((0 0, 4 0, 4 3, 0 3))")
    assert len(all_vertices(polygon)) == 5


def test_perfect_bipartite_match_rejects_two_left_one_right() -> None:
    edges = {"a": {"x"}, "b": {"x"}}
    assert perfect_bipartite_match(edges) is False
    assert perfect_bipartite_match({"a": {"x"}, "b": {"x", "y"}}) is True


def test_consistent_dataset_audits_with_range_and_ambiguity() -> None:
    dataset = load_dataset("examples/dataset_consistent.json")
    result = audit_batch(dataset)

    assert result.common_translation_possible is True
    assert len(result.allowed_translation_boxes) >= 1
    box = result.allowed_translation_boxes[0]
    assert box[0] <= 2.05 <= box[2]
    assert box[1] <= 1.0 <= box[3]
    assert result.unique_matches.get("F1") == "F1"
    assert result.unique_matches.get("F4") == "F4"

    unnamed_old = {key for key in result.ambiguous_matches if key.startswith("old#")}
    assert unnamed_old, "duplicate-shaped unlabelled outlines must stay ambiguous"
    for targets in result.ambiguous_matches.values():
        assert len(targets) == 2

    feasible_new: set[str] = set()
    for matching in result.feasible_matchings:
        assigned = [edge.new_id for edge in matching.edges]
        assert len(assigned) == len(set(assigned)), (
            "no two facilities may share one outline"
        )
        feasible_new.update(assigned)
    assert len(feasible_new) == len(dataset["new"])


def test_conflict_dataset_reports_mutually_exclusive_control_boxes() -> None:
    dataset = load_dataset("examples/dataset_conflict.json")
    result = audit_batch(dataset)

    assert result.common_translation_possible is False
    assert any(
        conflict["type"] == "control_box_mismatch"
        for conflict in result.conflicts
    )
    mismatch = next(
        conflict
        for conflict in result.conflicts
        if conflict["type"] == "control_box_mismatch"
    )
    labels = {evidence["label"] for evidence in mismatch["evidence"]}
    assert labels == {"id=F1", "id=F2"}

    f1_box = next(
        evidence["box"]
        for evidence in mismatch["evidence"]
        if evidence["label"] == "id=F1"
    )
    f2_box = next(
        evidence["box"]
        for evidence in mismatch["evidence"]
        if evidence["label"] == "id=F2"
    )
    assert intersect_boxes((tuple(f1_box), tuple(f2_box))) is None
    assert result.unmatched, "missing fourth new outline must be flagged"


def test_known_control_forces_assignment_against_duplicate_shape() -> None:
    dataset = {
        "tolerance": 0.1,
        "old": [
            {"id": "A", "geometry": "POLYGON((0 0, 1 0, 1 1, 0 1))"},
            {"id": "B", "geometry": "POLYGON((10 0, 11 0, 11 1, 10 1))"},
        ],
        "new": [
            {"id": "X", "geometry": "POLYGON((2 3, 3 3, 3 4, 2 4))"},
            {"id": "Y", "geometry": "POLYGON((12 3, 13 3, 13 4, 12 4))"},
        ],
        "known_pairs": [
            {"old_id": "A", "new_id": "X"},
            {"old_id": "B", "new_id": "X"},
        ],
    }
    result = audit_batch(dataset)
    assert result.common_translation_possible is False
    assert result.unmatched
    assert any(
        "one-to-one" in reason
        for reasons in result.unmatched.values()
        for reason in reasons
    )


def test_per_vertex_tolerance_sets_half_widths() -> None:
    dataset = {
        "tolerance": 0.2,
        "old": [{"id": "P", "geometry": "POINT(0 0)"}],
        "new": [{"id": "P", "geometry": "POINT(1 1)"}],
    }
    result = audit_batch(dataset)
    assert result.common_translation_possible is True
    box = result.allowed_translation_boxes[0]
    assert math.isclose(box[2] - box[0], 0.4)
    assert math.isclose(box[3] - box[1], 0.4)


def test_geojson_mapping_input_and_extra_new_outline() -> None:
    dataset = {
        "tolerance": 0.1,
        "old": [
            {
                "id": "G1",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                },
            }
        ],
        "new": [
            {
                "id": "G1",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[2, 2], [3, 2], [3, 3], [2, 3], [2, 2]]],
                },
            },
            {
                "id": "G9",
                "geometry": {
                    "type": "Point",
                    "coordinates": [50, 50],
                },
            },
        ],
    }
    result = audit_batch(dataset)
    assert result.common_translation_possible is True
    assert result.unique_matches == {"G1": "G1"}
    assert "G9" in result.unmatched


def test_empty_batches_trivially_allow_a_translation() -> None:
    result = audit_batch({"tolerance": 0.1, "old": [], "new": []})
    assert result.common_translation_possible is True
    assert result.allowed_translation_boxes == []

