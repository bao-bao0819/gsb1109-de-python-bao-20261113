"""Tests for the offline, hierarchical collection directory."""

import json

from click.testing import CliRunner
import pytest

import mercantile
from mercantile import Tile
from mercantile.coverage import OfflineCoverage, CoverageError, FALLBACK, STOP
from mercantile.scripts import cli


def publish(rev, tiles, source="default"):
    return {"rev": rev, "source": source, "action": "publish", "tiles": tiles}


def retract(rev, tiles, mode=FALLBACK, source="default"):
    return {
        "rev": rev,
        "source": source,
        "action": "retract",
        "retract_mode": mode,
        "tiles": tiles,
    }


def test_publish_root_then_deeper_supersedes():
    cov = OfflineCoverage()
    cov.apply(publish(1, [[0, 0, 0]]))
    assert cov.leaves() == [Tile(0, 0, 0)]

    cov.apply(publish(2, [[0, 0, 1]]))
    assert set(cov.leaves()) == set(mercantile.children(Tile(0, 0, 0)))


def test_fallback_retract_returns_to_parent():
    cov = OfflineCoverage()
    cov.apply(publish(1, [[0, 0, 0]]))
    cov.apply(publish(2, [[0, 0, 1]]))
    cov.apply(retract(3, [[0, 0, 1]], FALLBACK))
    assert cov.leaves() == [Tile(0, 0, 0)]


def test_fallback_preserves_independent_submap():
    cov = OfflineCoverage()
    cov.apply(publish(1, [[0, 0, 0]]))
    cov.apply(publish(2, [[1, 1, 2]]))
    cov.apply(retract(3, [[0, 0, 1]], FALLBACK))
    leaves = set(cov.leaves())
    assert Tile(1, 1, 2) in leaves
    # The withdrawn tile itself is no longer offered.
    assert Tile(0, 0, 1) not in leaves


def test_stop_retract_opens_a_hole():
    cov = OfflineCoverage()
    cov.apply(publish(1, [[0, 0, 0]]))
    cov.apply(retract(2, [[0, 0, 1]], STOP))
    leaves = set(cov.leaves())
    assert Tile(0, 0, 1) not in leaves
    assert Tile(1, 0, 1) in leaves
    assert not cov.contains([0, 0, 1])
    assert cov.contains([1, 0, 1])


def test_stop_removes_nested_submap():
    cov = OfflineCoverage()
    cov.apply(publish(1, [[0, 0, 0]]))
    cov.apply(publish(2, [[1, 1, 2]]))
    cov.apply(retract(3, [[0, 0, 1]], STOP))
    assert Tile(1, 1, 2) not in cov.leaves()
    assert not cov.contains([1, 1, 2])


def test_fallback_after_stop_restores_parent_coverage():
    cov = OfflineCoverage()
    cov.apply(publish(1, [[0, 0, 0]]))
    cov.apply(retract(2, [[0, 0, 1]], STOP))
    assert not cov.contains([0, 0, 1])
    cov.apply(retract(3, [[0, 0, 1]], FALLBACK))
    assert cov.contains([0, 0, 1])
    assert cov.leaves() == [Tile(0, 0, 0)]


def test_publish_inside_stopped_area_reopens_only_that_subarea():
    cov = OfflineCoverage()
    cov.apply(publish(1, [[0, 0, 0]]))
    cov.apply(retract(2, [[0, 0, 1]], STOP))
    cov.apply(publish(3, [[0, 0, 2]]))  # grandchild inside the stopped quadrant
    leaves = set(cov.leaves())
    assert Tile(0, 0, 2) in leaves
    assert cov.contains([0, 0, 2])
    # The rest of the stopped quadrant stays dark.
    assert not cov.contains([1, 0, 2])
    # Coverage outside the stopped quadrant is untouched.
    assert cov.contains([1, 0, 1])


def test_replay_is_idempotent():
    records = [
        publish(1, [[0, 0, 0]]),
        publish(2, [[0, 0, 1]]),
        retract(3, [[0, 0, 1]], FALLBACK),
    ]
    first = OfflineCoverage()
    first.apply_many(records)
    snapshot = first.leaves()

    # Replaying the same list into the same state must not drop another level.
    first.apply_many([dict(r) for r in records])
    assert first.leaves() == snapshot

    # A fresh collector replaying the list reaches the same coverage.
    replayed = OfflineCoverage()
    replayed.apply_many([dict(r) for r in records])
    assert replayed.leaves() == snapshot


def test_late_file_from_superseded_revision_is_ignored():
    cov = OfflineCoverage()
    cov.apply_many([publish(5, [[0, 0, 0]])])
    report = cov.apply_many([publish(3, [[1, 1, 1]])])
    assert len(report.stale) == 1
    assert report.applied == []
    assert Tile(1, 1, 1) not in cov.leaves()


def test_out_of_order_files_reconciled_by_revision():
    # rev 5 is listed before rev 2; the batch is reconciled by revision so the
    # newer root offer (rev 5) supersedes the deeper rev 2 tile.
    cov = OfflineCoverage()
    report = cov.apply_many(
        [publish(5, [[0, 0, 0]]), publish(2, [[0, 0, 1]])]
    )
    assert report.stale == []
    assert len(report.applied) == 2
    assert cov.leaves() == [Tile(0, 0, 0)]


def test_sources_track_revisions_independently():
    cov = OfflineCoverage()
    cov.apply(publish(5, [[0, 0, 0]], source="a"))
    # A different source is not gated by source "a"'s revision.
    cov.apply(publish(1, [[1, 0, 1]], source="b"))
    assert Tile(1, 0, 1) in cov.leaves()
    # But a late file from source "a" is still rejected.
    assert cov.apply(publish(4, [[1, 1, 1]], source="a")) is None


def test_change_report_lists_affected_leaf_ranges():
    cov = OfflineCoverage()
    first = cov.apply_many([publish(1, [[0, 0, 0]])])
    assert first.added == [Tile(0, 0, 0)]
    assert first.removed == []

    # Refining an already covered area does not gain or lose any coverage.
    refine = cov.apply_many([publish(2, [[0, 0, 1]])])
    assert refine.added == []
    assert refine.removed == []
    assert len(refine.applied) == 1

    # A stop removes exactly the stopped range, expressed at the common zoom.
    stopped = cov.apply_many([retract(3, [[0, 0, 1]], STOP)])
    assert stopped.removed == [Tile(0, 0, 1)]
    assert stopped.added == []
    assert stopped.affected == [Tile(0, 0, 1)]


def test_leaves_are_a_disjoint_antichain():
    cov = OfflineCoverage()
    operations = [
        publish(1, [[0, 0, 0]]),
        publish(2, [[0, 0, 1]]),
        publish(3, [[1, 1, 2]]),
        retract(4, [[0, 0, 1]], STOP),
        publish(5, [[0, 0, 2]]),
        retract(6, [[0, 0, 1]], FALLBACK),
        retract(7, [[1, 1, 2]], FALLBACK),
    ]
    for record in operations:
        cov.apply(record)
        leaves = cov.leaves()
        for earlier in leaves:
            for later in leaves:
                if earlier == later:
                    continue
                # No leaf may contain another leaf.
                assert not (
                    later.z > earlier.z
                    and mercantile.parent(later, zoom=earlier.z) == earlier
                )


def test_invalid_records_raise():
    cov = OfflineCoverage()
    with pytest.raises(CoverageError):
        cov.apply({"action": "publish", "tiles": [[0, 0, 0]]})
    with pytest.raises(CoverageError):
        cov.apply({"rev": 1, "action": "explode", "tiles": [[0, 0, 0]]})
    with pytest.raises(CoverageError):
        cov.apply(
            {
                "rev": 1,
                "action": "retract",
                "retract_mode": "nope",
                "tiles": [[0, 0, 1]],
            }
        )


def test_cli_offline_publish_and_leaves():
    runner = CliRunner()
    result = runner.invoke(cli, ["offline"], '{"rev": 1, "tiles": [[0, 0, 0]]}')
    assert result.exit_code == 0
    assert result.output == "[0, 0, 0]\n"


def test_cli_offline_stop_mode():
    runner = CliRunner()
    stream = (
        '{"rev": 1, "tiles": [[0, 0, 0]]}\n'
        '{"rev": 2, "action": "retract", "tiles": [[0, 0, 1]]}\n'
    )
    fallback = runner.invoke(cli, ["offline"], stream)
    stopped = runner.invoke(cli, ["offline", "--mode", STOP], stream)
    assert fallback.exit_code == 0 and stopped.exit_code == 0
    assert json.loads(fallback.output.strip()) == [0, 0, 0]
    stopped_tiles = [json.loads(line) for line in stopped.output.splitlines()]
    assert [0, 0, 1] not in stopped_tiles
    assert len(stopped_tiles) == 3


def test_cli_offline_report():
    runner = CliRunner()
    result = runner.invoke(
        cli, ["offline", "--report"], '{"rev": 1, "tiles": [[0, 0, 0]]}'
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["added"] == [[0, 0, 0]]
    assert payload["affected"] == [[0, 0, 0]]


def test_cli_offline_replay_does_not_degrade():
    runner = CliRunner()
    stream = (
        '{"rev": 1, "tiles": [[0, 0, 0]]}\n'
        '{"rev": 2, "action": "retract", "retract_mode": "fallback",'
        ' "tiles": [[0, 0, 1]]}\n'
    )
    once = runner.invoke(cli, ["offline"], stream)
    twice = runner.invoke(cli, ["offline"], stream + stream)
    assert once.exit_code == 0 and twice.exit_code == 0
    assert once.output == twice.output


def test_cli_offline_directory(tmp_path):
    (tmp_path / "01.json").write_text(
        json.dumps(publish(1, [[0, 0, 0]]))
    )
    (tmp_path / "02.json").write_text(
        json.dumps(
            [
                publish(2, [[0, 0, 1]]),
                retract(3, [[0, 0, 1]], FALLBACK),
            ]
        )
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["offline", str(tmp_path)])
    assert result.exit_code == 0
    assert json.loads(result.output.strip()) == [0, 0, 0]


def test_cli_offline_bad_record_exits_nonzero():
    runner = CliRunner()
    result = runner.invoke(
        cli, ["offline"], '{"rev": 1, "action": "frobnicate", "tiles": [[0, 0, 0]]}'
    )
    assert result.exit_code == 2
