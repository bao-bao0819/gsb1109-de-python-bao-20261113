import pytest

from referencing import (
    Match,
    MigrationReport,
    PointerMigrator,
    Status,
    exceptions,
)
from referencing.migration import build, escape, parse, resolve

OLD_DOCUMENT = {
    "contract": {
        "parties": {
            "ACME/001": {"name": "Acme", "rating": 3},
            "a~b": {"name": "AtildeB", "rating": 5},
        },
        "meta": {
            "owner": "alice",
            "code": 4242,
            "enabled": True,
        },
        "tags": {"color": "green"},
        "legacy": "gone-field",
        "dropped": {"x": 1},
    },
}

NEW_DOCUMENT = {
    "contract": {
        "parties": [
            {"partyId": "a~b", "name": "AtildeB", "rating": 5},
            {"partyId": "ACME/001", "name": "Acme", "rating": 3},
        ],
        "meta": {
            "responsible": "alice",
            "identifier": 4242,
            "enabled": 1,
        },
        "labels": ["green", "green"],
        "legacy": "repurposed-content",
    },
}

MAPPING = {
    "/contract/parties": {
        "action": "folded",
        "to": "/contract/parties",
        "key": "partyId",
        "source": "changelog-v2",
    },
    "/contract/meta/owner": {
        "action": "moved",
        "to": "/contract/meta/responsible",
    },
    "/contract/dropped": None,
    "/contract/legacy": {"action": "deleted"},
}


def migrator(mapping=MAPPING):
    return PointerMigrator.create(
        old_document=OLD_DOCUMENT,
        new_document=NEW_DOCUMENT,
        mapping=mapping,
    )


class TestPointerSyntax:
    @pytest.mark.parametrize(
        ("segment", "escaped"),
        [
            ("plain", "plain"),
            ("a/b", "a~1b"),
            ("a~b", "a~0b"),
            ("a/b~c", "a~1b~0c"),
            ("/", "~1"),
            ("~", "~0"),
            ("~1", "~01"),
            ("~0", "~00"),
            ("", ""),
        ],
    )
    def test_escape_roundtrip(self, segment, escaped):
        assert escape(segment) == escaped
        pointer = "/" + escaped
        assert parse(pointer) == (segment,)
        assert build((segment,)) == pointer

    def test_tilde_escaped_before_slash(self):
        assert escape("~/") == "~0~1"
        assert parse("/~0~1") == ("~/",)

    def test_root_pointer(self):
        assert parse("") == ()
        assert build(()) == ""

    def test_multiple_tokens_with_special_names(self):
        pointer = "/a~1b/c~0d/e"
        assert parse(pointer) == ("a/b", "c~d", "e")
        assert build(("a/b", "c~d", "e")) == pointer

    @pytest.mark.parametrize("pointer", ["/a~2b", "/a~", "no-leading-slash"])
    def test_invalid_pointer(self, pointer):
        with pytest.raises(exceptions.InvalidPointer):
            parse(pointer)

    def test_resolve_through_mapping_and_array(self):
        document = {"items": [{"k": "v"}]}
        assert resolve(document, "/items/0/k") == "v"

    def test_resolve_missing(self):
        with pytest.raises(LookupError):
            resolve({"items": []}, "/items/3/k")

    def test_resolve_rejects_leading_zero_index(self):
        with pytest.raises(LookupError):
            resolve({"items": [1]}, "/items/01")


class TestMigration:
    def test_fold_follows_business_identifier_with_slash(self):
        report = migrator().migrate_one(
            "/contract/parties/ACME~1001/rating",
        )
        assert report.status is Status.unique
        assert report.new_pointer == "/contract/parties/1/rating"
        assert report.old_value == 3
        assert report.new_value == 3
        assert report.sources == ("changelog-v2",)

    def test_fold_follows_business_identifier_with_tilde(self):
        report = migrator().migrate_one(
            "/contract/parties/a~0b/name",
        )
        assert report.status is Status.unique
        assert report.new_pointer == "/contract/parties/0/name"
        assert report.new_value == "AtildeB"

    def test_fold_collection_itself_points_at_array(self):
        report = migrator().migrate_one("/contract/parties")
        assert report.status is Status.unique
        assert report.new_pointer == "/contract/parties"
        assert report.new_value == NEW_DOCUMENT["contract"]["parties"]

    def test_fold_unknown_identifier_is_deleted(self):
        report = migrator().migrate_one(
            "/contract/parties/missing~1id/rating",
        )
        assert report.status is Status.deleted
        assert report.new_pointer is None
        assert "missing/id" in report.reason

    def test_fold_removed_subfield_is_deleted(self):
        report = migrator().migrate_one(
            "/contract/parties/ACME~1001/removed",
        )
        assert report.status is Status.deleted
        assert report.candidates == ()

    def test_duplicate_identifiers_are_ambiguous(self):
        new_document = {
            "contract": {
                "parties": [
                    {"partyId": "ACME/001", "rating": 1},
                    {"partyId": "ACME/001", "rating": 2},
                ],
            },
        }
        migrated = PointerMigrator.create(
            old_document=OLD_DOCUMENT,
            new_document=new_document,
            mapping=MAPPING,
        ).migrate_one("/contract/parties/ACME~1001/rating")
        assert migrated.status is Status.ambiguous
        assert {candidate.pointer for candidate in migrated.candidates} == {
            "/contract/parties/0/rating",
            "/contract/parties/1/rating",
        }

    def test_explicit_move(self):
        report = migrator().migrate_one("/contract/meta/owner")
        assert report.status is Status.unique
        assert report.new_pointer == "/contract/meta/responsible"
        assert report.old_value == report.new_value == "alice"

    def test_mapping_target_missing_is_reported_deleted(self):
        mapping = {"/contract/meta/owner": "/contract/meta/nonexistent"}
        report = migrator(mapping).migrate_one("/contract/meta/owner")
        assert report.status is Status.deleted
        assert "does not resolve" in report.reason

    @pytest.mark.parametrize("mapping", [
        {"/contract/dropped": None},
        {"/contract/dropped": {"action": "deleted"}},
    ])
    def test_deleted_directives(self, mapping):
        report = migrator(mapping).migrate_one("/contract/dropped")
        assert report.status is Status.deleted
        assert report.old_value == {"x": 1}

    def test_untouched_equal_location_is_unique(self):
        old = {"k": {"same": 7}}
        new = {"k": {"same": 7, "other": 8}}
        report = PointerMigrator.create(old, new, {}).migrate_one(
            "/k/same",
        )
        assert report.status is Status.unique
        assert report.new_pointer == "/k/same"
        assert report.sources == ("value-comparison",)

    def test_repurposed_location_is_not_pretended_success(self):
        report = migrator().migrate_one("/contract/legacy")
        assert report.status is Status.deleted  # explicit mapping wins

        old = {"k": "old-business-field"}
        new = {"k": "different-business-field"}
        report = PointerMigrator.create(old, new, {}).migrate_one("/k")
        assert report.status is Status.repurposed
        assert report.new_pointer is None
        assert report.old_value == "old-business-field"
        assert report.new_value == "different-business-field"
        assert not report.matched

    def test_bool_is_not_treated_as_integer_one(self):
        report = migrator({}).migrate_one("/contract/meta/enabled")
        assert report.status is Status.repurposed
        assert report.old_value is True
        assert report.new_value == 1

    def test_unique_scalar_relocated_by_value(self):
        report = migrator({}).migrate_one("/contract/meta/code")
        assert report.status is Status.unique
        assert report.new_pointer == "/contract/meta/identifier"
        assert report.new_value == 4242

    def test_duplicate_scalar_values_are_ambiguous(self):
        report = migrator({}).migrate_one("/contract/tags/color")
        assert report.status is Status.ambiguous
        assert len(report.candidates) == 2
        assert all(isinstance(candidate, Match) for candidate in
                   report.candidates)
        pointers = {candidate.pointer for candidate in report.candidates}
        assert pointers == {
            "/contract/labels/0",
            "/contract/labels/1",
        }

    def test_dangling_old_pointer(self):
        report = migrator().migrate_one("/contract/nope/missing")
        assert report.status is Status.dangling
        assert report.new_pointer is None

    def test_container_value_without_mapping_is_ambiguous(self):
        old = {"thing": {"nested": [1, 2]}}
        new = {"copy": {"nested": [1, 2]}, "other": {"nested": [1, 2]}}
        report = PointerMigrator.create(old, new, {}).migrate_one("/thing")
        assert report.status is Status.ambiguous

    def test_many_preserves_order_and_deduplicates(self):
        pointers = [
            "/contract/parties/ACME~1001/rating",
            "/contract/legacy",
            "/contract/parties/ACME~1001/rating",
            "/contract/nope",
        ]
        reports = migrator().migrate(pointers)
        assert len(reports) == 3
        assert [report.old_pointer for report in reports] == [
            "/contract/parties/ACME~1001/rating",
            "/contract/legacy",
            "/contract/nope",
        ]
        assert all(isinstance(report, MigrationReport) for report in reports)

    def test_every_report_keeps_evidence(self):
        report = migrator().migrate_one(
            "/contract/parties/a~0b/rating",
        )
        assert report.old_pointer == "/contract/parties/a~0b/rating"
        assert report.reason
        assert report.sources
        assert report.candidates[0].pointer == report.new_pointer


class TestInvalidMappings:
    @pytest.mark.parametrize("entry", [
        {"action": "moved"},
        {"action": "frozen"},
        {"action": "folded", "to": "/x"},
        {"action": "folded", "key": "id"},
        123,
    ])
    def test_invalid_entries(self, entry):
        with pytest.raises(exceptions.InvalidMigration):
            PointerMigrator.create(
                OLD_DOCUMENT,
                NEW_DOCUMENT,
                {"/contract": entry},
            )

    def test_invalid_pointer_key(self):
        with pytest.raises(exceptions.InvalidPointer):
            PointerMigrator.create(
                OLD_DOCUMENT,
                NEW_DOCUMENT,
                {"no-leading-slash": "/x"},
            )

    def test_invalid_pointer_queried(self):
        with pytest.raises(exceptions.InvalidPointer):
            migrator().migrate_one("no-leading-slash")

    def test_source_labels_are_validated(self):
        with pytest.raises(exceptions.InvalidMigration):
            PointerMigrator.create(
                OLD_DOCUMENT,
                NEW_DOCUMENT,
                {"/contract": {"action": "deleted", "source": 3}},
            )
