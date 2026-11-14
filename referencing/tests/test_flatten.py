from pathlib import Path
import json

import pytest

from referencing import flatten


SUITE = Path(__file__).parent.parent.parent / "suite"
OFFLINE = Path(__file__).parent.parent.parent / "offline-package"


@pytest.fixture(scope="module")
def migration():
    assert SUITE.is_dir(), "the suite submodule must be checked out"
    migration, verification = flatten.build(SUITE, OFFLINE, force=True)
    assert verification.ok, verification.failures
    return migration


def test_every_source_resource_has_one_flat_entry(migration):
    sources = {
        path.relative_to(SUITE).as_posix()
        for path in (SUITE / "tests").glob("*/**/*.json")
        if path.name != "specifications.json"
    }
    assert {entry.source for entry in migration.entries} == sources


def test_flat_names_are_unique(migration):
    names = [entry.flat for entry in migration.entries]
    assert len(names) == len(set(names))


def test_names_include_the_original_draft_not_only_the_basename(migration):
    names = {entry.flat for entry in migration.entries}
    assert "multiple-lookup--07.json" in names
    assert "multiple-lookup--2020-12.json" in names
    assert "multiple-lookup.json" not in names


def test_aliases_remain_distinct_entries(migration):
    aliases = {
        member
        for group in migration.aliases
        for member in group.members
    }
    assert aliases
    for group in migration.aliases:
        assert len(group.members) == len(set(group.members))
        assert all(member in aliases for member in group.members)


def test_a_basename_with_three_way_distinct_content_is_not_merged(migration):
    names = [
        entry.flat for entry in migration.entries
        if entry.source.endswith("/keywords-items-object.json")
        or entry.source.endswith("/keywords-items.json")
    ]
    assert len(names) == 6
    assert len(set(names)) == 6


def test_only_the_local_control_link_is_rewritten(migration):
    for entry in migration.entries:
        original = (SUITE / entry.source).read_text(encoding="utf-8")
        migrated = (OFFLINE / entry.flat).read_text(encoding="utf-8")
        assert migrated == original.replace(
            flatten.SOURCE_LINK,
            flatten.FLAT_LINK,
        )


def test_external_identities_are_unchanged(migration):
    assert "http://example.com/foo#" in migration.external_identities
    assert "urn:example:schema" in migration.external_identities
    assert not any(
        identity.startswith(("#", ".", "/"))
        for identity in migration.external_identities
    )


def test_verification_reaches_the_same_business_definition(migration):
    verification = flatten.verify(migration)
    assert verification.assertions == 538
    assert verification.expectations == 335
    assert verification.links_checked == 296
    assert verification.ok, verification.failures


def test_verification_report_is_part_of_the_package():
    report = json.loads((OFFLINE / "verification.json").read_text())
    assert report["verified"] is True
    assert report["failures"] == []


def test_manifest_maps_both_directions(migration):
    manifest = json.loads((OFFLINE / "migration.json").read_text())
    assert len(manifest["entries"]) == len(migration.entries)
    entries = {
        entry["source"]: entry["target"] for entry in manifest["entries"]
    }
    assert entries["tests/json-schema-draft-07/multiple-lookup.json"] == (
        "multiple-lookup--07.json"
    )
    assert {
        entry["target"]: entry["source"] for entry in manifest["entries"]
    } == {target: source for source, target in entries.items()}
