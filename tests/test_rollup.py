"""Tests for rolling child tile device counts up to a parent tile."""

import pytest

import mercantile
from mercantile import (
    BoundTerm,
    Bounds,
    EvidenceStatus,
    Rollup,
    TileEvidence,
    rollup,
)


PARENT = mercantile.Tile(0, 0, 0)
KIDS = mercantile.children(PARENT)


def make_children(observed=None, counts=None, missing=(), empty=()):
    """Build TileEvidence entries keyed by child index."""
    observed = observed or {}
    counts = counts or {}
    entries = []
    for index, devices in observed.items():
        entries.append(TileEvidence.observed(KIDS[index], devices))
    for index, total in counts.items():
        entries.append(TileEvidence.count_only(KIDS[index], total))
    for index in missing:
        entries.append(TileEvidence.missing(KIDS[index]))
    for index in empty:
        entries.append(TileEvidence.empty(KIDS[index]))
    return entries


def test_all_observed_deduplicates_boundary_devices():
    # Devices on a shared boundary appear in several child lists but
    # must be counted once in the parent.
    entries = make_children(
        observed={
            0: {"a", "b"},
            1: {"b", "c"},
            2: {"c", "d"},
            3: {"a", "d"},
        }
    )
    result = rollup(PARENT, entries)
    assert isinstance(result, Rollup)
    assert result.exact is True
    assert result.bounds == Bounds(4, 4)
    assert result.bounds.is_exact
    assert result.known_devices == {"a", "b", "c", "d"}


def test_same_devices_in_all_children_is_not_counted_four_times():
    entries = make_children(
        observed={0: {"a"}, 1: {"a"}, 2: {"a"}, 3: {"a"}}
    )
    result = rollup(PARENT, entries)
    assert result.bounds == Bounds(1, 1)
    assert len(result.known_devices) == 1


def test_confirmed_empty_differs_from_missing():
    empty_entry = TileEvidence.empty(KIDS[0])
    assert empty_entry.status is EvidenceStatus.CONFIRMED_EMPTY
    assert empty_entry.count == 0
    assert empty_entry.devices is None

    missing_entry = TileEvidence.missing(KIDS[1])
    assert missing_entry.status is EvidenceStatus.MISSING
    assert missing_entry.count is None
    assert missing_entry.devices is None


def test_confirmed_empty_children_contribute_nothing():
    entries = make_children(
        observed={0: {"a", "b"}}, empty=(1, 2, 3)
    )
    result = rollup(PARENT, entries)
    assert result.exact is True
    assert result.bounds == Bounds(2, 2)
    statuses = {item.tile: item.status for item in result.children}
    assert all(
        statuses[KIDS[i]] is EvidenceStatus.CONFIRMED_EMPTY for i in (1, 2, 3)
    )


def test_unreported_children_are_filled_in_as_missing():
    entries = make_children(observed={0: {"a"}})
    result = rollup(PARENT, entries)
    statuses = [item.status for item in result.children]
    assert statuses.count(EvidenceStatus.MISSING) == 3
    assert result.exact is False


def test_count_only_child_gives_evidence_backed_bounds():
    # One observed tile with 2 devices, one count-only tile with 5.
    entries = make_children(
        observed={0: {"a", "b"}}, counts={1: 5}, empty=(2, 3)
    )
    result = rollup(PARENT, entries)
    assert result.exact is False
    # Lower: at least the largest of (2 observed, 5 count-only).
    # Upper: fully disjoint case, 2 + 5.
    assert result.bounds == Bounds(5, 7)


def test_missing_child_is_capped_by_smallest_positive_sibling():
    entries = make_children(
        observed={0: {"a", "b"}}, counts={1: 5}, missing=(2,), empty=(3,)
    )
    result = rollup(PARENT, entries)
    # Missing tile cannot hold anything outside its siblings' reach, so
    # its term is capped at min(2, 5) = 2.
    assert result.bounds == Bounds(5, 9)
    missing_terms = [
        term for term in result.upper_terms if term.kind == "missing"
    ]
    assert len(missing_terms) == 1
    term = missing_terms[0]
    assert term.tile == KIDS[2]
    assert term.amount == 2
    assert set(term.capped_by) == {KIDS[0], KIDS[1]}


def test_missing_child_without_positive_evidence_is_unbounded():
    entries = make_children(observed={0: set()}, missing=(1,), empty=(2, 3))
    result = rollup(PARENT, entries)
    assert result.bounds.lower == 0
    assert result.bounds.upper is None
    missing_terms = [
        term for term in result.upper_terms if term.kind == "missing"
    ]
    assert missing_terms[0].amount is None
    assert missing_terms[0].capped_by == ()


def test_all_missing_is_fully_unbounded():
    result = rollup(PARENT, [])
    assert result.bounds == Bounds(0, None)
    assert all(
        item.status is EvidenceStatus.MISSING for item in result.children
    )


def test_upper_terms_break_down_every_contribution():
    entries = make_children(
        observed={0: {"a"}}, counts={1: 4}, missing=(2,), empty=(3,)
    )
    result = rollup(PARENT, entries)
    kinds = [term.kind for term in result.upper_terms]
    assert kinds == ["known_union", "count_only", "missing"]
    amounts = [term.amount for term in result.upper_terms]
    # 1 observed + 4 count-only + cap of min(1, 4) = 1.
    assert amounts == [1, 4, 1]
    assert result.bounds.upper == 6
    # The missing term exposes the evidence it depends on.
    missing_term = result.upper_terms[-1]
    assert isinstance(missing_term, BoundTerm)
    assert missing_term.capped_by == (KIDS[0], KIDS[1])


def test_explain_upper_lists_missing_evidence_dependency():
    entries = make_children(
        observed={0: {"a"}}, counts={1: 4}, missing=(2,), empty=(3,)
    )
    result = rollup(PARENT, entries)
    text = "\n".join(result.explain_upper())
    assert str(KIDS[2]) in text
    assert "capped" in text


def test_explain_upper_reports_unbounded_missing_tile():
    entries = make_children(missing=(0,), empty=(1, 2, 3))
    result = rollup(PARENT, entries)
    text = "\n".join(result.explain_upper())
    assert "unbounded" in text


def test_lower_bound_never_below_observed_union():
    entries = make_children(
        observed={0: {"a", "b", "c"}}, counts={1: 2}, empty=(2, 3)
    )
    result = rollup(PARENT, entries)
    # Count-only set of 2 can be fully inside the observed union of 3.
    assert result.bounds == Bounds(3, 5)


def test_observed_evidence_validates_count_mismatch():
    with pytest.raises(ValueError):
        TileEvidence(
            KIDS[0],
            EvidenceStatus.OBSERVED,
            devices=frozenset({"a"}),
            count=2,
        )


def test_count_only_rejects_negative_and_non_integer():
    with pytest.raises(ValueError):
        TileEvidence.count_only(KIDS[0], -1)
    with pytest.raises(TypeError):
        TileEvidence.count_only(KIDS[0], 1.5)


def test_count_only_rejects_device_list():
    with pytest.raises(ValueError):
        TileEvidence(
            KIDS[0], EvidenceStatus.COUNT_ONLY, devices={"a"}, count=1
        )


def test_missing_and_empty_reject_data():
    with pytest.raises(ValueError):
        TileEvidence(KIDS[0], EvidenceStatus.MISSING, count=1)
    with pytest.raises(ValueError):
        TileEvidence(KIDS[0], EvidenceStatus.CONFIRMED_EMPTY, devices={"a"})


def test_child_must_belong_to_parent():
    other = mercantile.Tile(0, 0, 2)
    assert mercantile.parent(other) != PARENT
    with pytest.raises(ValueError):
        rollup(PARENT, [TileEvidence.observed(other, {"a"})])


def test_duplicate_child_evidence_is_rejected():
    entries = [
        TileEvidence.observed(KIDS[0], {"a"}),
        TileEvidence.count_only(KIDS[0], 1),
    ]
    with pytest.raises(ValueError):
        rollup(PARENT, entries)


def test_parent_derived_from_children_when_omitted():
    entries = make_children(observed={0: {"a"}}, empty=(1, 2, 3))
    result = rollup(None, entries)
    assert result.tile == PARENT


def test_dict_evidence_is_accepted():
    entries = [
        {
            "tile": KIDS[0],
            "status": EvidenceStatus.OBSERVED,
            "devices": {"a", "b"},
        },
        {"tile": KIDS[1], "status": EvidenceStatus.CONFIRMED_EMPTY},
    ]
    result = rollup(PARENT, entries)
    assert result.bounds.lower == 2
    assert result.children[1].status is EvidenceStatus.CONFIRMED_EMPTY


def test_rollup_exported_from_package():
    assert mercantile.rollup is rollup
    assert mercantile.TileEvidence is TileEvidence
    assert mercantile.EvidenceStatus is EvidenceStatus
