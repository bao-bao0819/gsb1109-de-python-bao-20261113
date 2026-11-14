from pathlib import Path
import json

import pytest

from referencing import Registry, Resource, Specification
from referencing.audit import (
    AuditReport,
    LoadError,
    Occurrence,
    check_entry,
    check_package,
    format_report,
    main,
)
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).parent.parent.parent
CONTRACTS = ROOT / "vendor-contracts"
SCHEMA = "https://json-schema.org/draft/2020-12/schema"
MONEY_SCOPE = "https://vendor.example/schemas/shared/money.json"


def write(path: Path, contents: dict) -> None:
    path.write_text(json.dumps(contents))


class TestCheckPackage:
    def test_conflicting_embedded_anchor(self):
        report = check_package(CONTRACTS)

        assert not report.ok()
        (anchor_conflict,) = report.anchor_conflicts
        assert anchor_conflict.name == "amount"
        assert anchor_conflict.scope == MONEY_SCOPE
        assert anchor_conflict.entries == (
            str(CONTRACTS / "invoices.json"),
            str(CONTRACTS / "orders.json"),
        )
        pointers = {each.pointer for each in anchor_conflict.occurrences}
        assert pointers == {"/$defs/money"}
        assert len(anchor_conflict.definitions) == 2

    def test_embedded_resource_uri_also_conflicts(self):
        report = check_package(CONTRACTS)

        (resource_conflict,) = report.resource_conflicts
        assert resource_conflict.uri == MONEY_SCOPE
        assert set(resource_conflict.entries) == {
            str(CONTRACTS / "invoices.json"),
            str(CONTRACTS / "orders.json"),
        }
        assert len(resource_conflict.definitions) == 2

    def test_scope_matches_registry_crawl(self):
        resources = [
            Resource.from_contents(
                json.loads(path.read_text()),
                default_specification=DRAFT202012,
            )
            for path in CONTRACTS.glob("*.json")
        ]
        registry = resources @ Registry()

        assert (MONEY_SCOPE, "amount") in registry.crawl()._anchors

    def test_same_anchor_name_in_another_scope_is_ignored(self):
        report = check_package(CONTRACTS)

        billing = str(CONTRACTS / "billing.json")
        assert report.for_entry(billing).ok()
        scopes = {conflict.scope for conflict in report.anchor_conflicts}
        assert "https://vendor.example/schemas/billing.json" not in scopes

    def test_identical_embedded_definitions_do_not_conflict(self, tmp_path):
        money = {
            "$id": "shared/money.json",
            "$anchor": "amount",
            "type": "object",
            "properties": {"value": {"type": "number"}},
        }
        for name in ("invoices", "orders"):
            write(
                tmp_path / f"{name}.json",
                {
                    "$schema": SCHEMA,
                    "$id": f"https://vendor.example/{name}.json",
                    "$defs": {"money": dict(money)},
                },
            )

        assert check_package(tmp_path).ok()

    def test_anchors_without_ids_stay_isolated_per_entry(self, tmp_path):
        write(
            tmp_path / "one.json",
            {
                "$schema": SCHEMA,
                "properties": {
                    "x": {"$anchor": "amount", "type": "integer"},
                },
            },
        )
        write(
            tmp_path / "two.json",
            {
                "$schema": SCHEMA,
                "properties": {
                    "y": {"$anchor": "amount", "type": "string"},
                },
            },
        )

        assert check_package(tmp_path).ok()

    def test_conflicting_anchors_within_one_scopeless_entry(self, tmp_path):
        write(
            tmp_path / "one.json",
            {
                "$schema": SCHEMA,
                "properties": {
                    "x": {"$anchor": "amount", "type": "integer"},
                    "y": {"$anchor": "amount", "type": "string"},
                },
            },
        )

        report = check_package(tmp_path)
        (conflict,) = report.anchor_conflicts
        assert conflict.scope is None
        assert conflict.entries == (str(tmp_path / "one.json"),)

    def test_invalid_json_is_a_load_error(self, tmp_path):
        broken = tmp_path / "broken.json"
        broken.write_text("{not json")

        report = check_package(tmp_path)
        assert not report.ok()
        (error,) = report.load_errors
        assert isinstance(error, LoadError)
        assert error.entry == str(broken)
        assert report.entries() == (str(broken),)
        assert report.for_entry(str(broken)).load_errors == (error,)
        assert "Could not load" in format_report(report)

    def test_non_mapping_document_is_crawled(self, tmp_path):
        write(tmp_path / "bare.json", True)

        report = check_package(tmp_path)
        assert report.ok()

    def test_anchors_inside_array_subresources(self, tmp_path):
        write(
            tmp_path / "branches.json",
            {
                "$schema": SCHEMA,
                "$id": "https://vendor.example/branches.json",
                "allOf": [
                    {"$anchor": "amount", "type": "integer"},
                    {
                        "anyOf": [
                            {"$anchor": "amount", "type": "string"},
                        ],
                    },
                ],
            },
        )

        report = check_package(tmp_path)
        (conflict,) = report.anchor_conflicts
        pointers = {occurrence.pointer for occurrence in conflict.occurrences}
        assert pointers == {"/allOf/0", "/allOf/1/anyOf/0"}

    def test_unknown_dialect_is_a_load_error(self, tmp_path):
        write(
            tmp_path / "odd.json",
            {"$schema": "https://example.invalid/dialect"},
        )

        report = check_package(
            tmp_path,
            default_specification=Specification,
        )
        (error,) = report.load_errors
        assert error.entry == str(tmp_path / "odd.json")

    def test_root_level_collision_renders_document_root(self, tmp_path):
        for name, kind in (("a", "integer"), ("b", "string")):
            write(
                tmp_path / f"{name}.json",
                {
                    "$schema": SCHEMA,
                    "$id": "https://vendor.example/shared.json",
                    "$anchor": "amount",
                    "type": kind,
                },
            )

        report = check_package(tmp_path)
        (conflict,) = report.anchor_conflicts
        assert {occurrence.pointer for occurrence in conflict.occurrences} == {
            "",
        }
        text = format_report(report)
        assert "a.json#/" in text
        assert len(conflict.definitions) == 2

    def test_duplicate_definitions_are_deduplicated(self, tmp_path):
        good = {
            "$schema": SCHEMA,
            "$id": "https://vendor.example/shared.json",
            "$anchor": "amount",
            "type": "integer",
        }
        bad = dict(good, type="string")
        write(tmp_path / "a.json", good)
        write(tmp_path / "b.json", dict(good))
        write(tmp_path / "c.json", bad)

        report = check_package(tmp_path)
        (conflict,) = report.anchor_conflicts
        assert len(conflict.occurrences) == 3
        assert len(conflict.definitions) == 2

    def test_missing_directory(self):
        with pytest.raises(NotADirectoryError):
            check_package(ROOT / "does-not-exist")


class TestForEntry:
    def test_lists_only_entries_that_meet_the_bad_definition(self):
        report = check_package(CONTRACTS)
        invoices = str(CONTRACTS / "invoices.json")
        orders = str(CONTRACTS / "orders.json")
        billing = str(CONTRACTS / "billing.json")

        assert report.entries() == tuple(sorted((invoices, orders)))

        for entry in (invoices, orders):
            subset = report.for_entry(entry)
            assert len(subset.anchor_conflicts) == 1
            assert len(subset.resource_conflicts) == 1
            assert entry in subset.anchor_conflicts[0].entries

        assert isinstance(report.for_entry(billing), AuditReport)
        assert report.for_entry(billing).ok()

    def test_fixing_base_uri_rechecks_affected_entries(self, tmp_path):
        for source in CONTRACTS.glob("*.json"):
            (tmp_path / source.name).write_text(source.read_text())

        before = check_package(tmp_path)
        assert not before.ok()
        assert set(before.entries()) == {
            str(tmp_path / "invoices.json"),
            str(tmp_path / "orders.json"),
        }

        orders = tmp_path / "orders.json"
        contents = json.loads(orders.read_text())
        contents["$defs"]["money"]["$id"] = "shared/money-v2.json"
        write(orders, contents)

        after = check_package(tmp_path)
        assert after.ok()
        assert after.entries() == ()

    def test_fixing_one_side_is_enough_to_clear_the_scope(self, tmp_path):
        for source in CONTRACTS.glob("*.json"):
            (tmp_path / source.name).write_text(source.read_text())

        invoices = tmp_path / "invoices.json"
        contents = json.loads(invoices.read_text())
        contents["$defs"]["money"]["$id"] = "shared/money-v2.json"
        write(invoices, contents)

        assert check_package(tmp_path).ok()


class TestCheckEntry:
    def test_collects_scopes_and_pointers(self):
        anchors, resources, error = check_entry(
            CONTRACTS / "invoices.json",
        )

        assert error is None
        (occurrence,) = anchors[(MONEY_SCOPE, "amount")]
        assert occurrence.pointer == "/$defs/money"
        assert occurrence.entry == str(CONTRACTS / "invoices.json")
        assert MONEY_SCOPE in resources

    def test_relative_id_resolves_against_parent_base(self, tmp_path):
        write(
            tmp_path / "nested.json",
            {
                "$schema": SCHEMA,
                "$id": "https://vendor.example/base/root.json",
                "$defs": {
                    "inner": {
                        "$id": "child/money.json",
                        "$anchor": "amount",
                        "type": "integer",
                    },
                },
            },
        )

        anchors, _, error = check_entry(tmp_path / "nested.json")
        assert error is None
        assert (
            "https://vendor.example/base/child/money.json",
            "amount",
        ) in anchors

    def test_nested_id_without_root_id_resolves_absolutely(self, tmp_path):
        write(
            tmp_path / "rootless.json",
            {
                "$schema": SCHEMA,
                "$defs": {
                    "inner": {
                        "$id": "https://vendor.example/child/money.json",
                        "$anchor": "amount",
                        "type": "integer",
                    },
                },
            },
        )

        anchors, resources, error = check_entry(tmp_path / "rootless.json")
        assert error is None
        scope = "https://vendor.example/child/money.json"
        assert (scope, "amount") in anchors
        assert scope in resources


class TestFormatReport:
    def test_reports_scope_and_entries(self):
        text = format_report(check_package(CONTRACTS))

        assert "amount" in text
        assert MONEY_SCOPE in text
        assert "invoices.json" in text
        assert "orders.json" in text
        assert "/$defs/money" in text

    def test_clean_report(self, tmp_path):
        write(
            tmp_path / "fine.json",
            {"$schema": SCHEMA, "$id": "https://v.example/fine.json"},
        )

        text = format_report(check_package(tmp_path))
        assert "No conflicts found" in text


class TestCli:
    def test_exit_nonzero_and_message_on_conflict(self, capsys):
        assert main([str(CONTRACTS)]) == 1
        out = capsys.readouterr().out
        assert "amount" in out
        assert MONEY_SCOPE in out

    def test_exit_zero_on_clean_package(self, tmp_path, capsys):
        write(
            tmp_path / "fine.json",
            {"$schema": SCHEMA, "$id": "https://v.example/fine.json"},
        )

        assert main([str(tmp_path)]) == 0
        assert "No conflicts found" in capsys.readouterr().out

    def test_usage_error_without_arguments(self, capsys):
        assert main([]) == 2
        assert "usage" in capsys.readouterr().err

    def test_main_reads_sys_argv(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["audit", str(CONTRACTS)])

        assert main() == 1
        assert "amount" in capsys.readouterr().out


class TestModels:
    def test_occurrences_are_value_objects(self):
        first = Occurrence(
            entry="a.json",
            pointer="/$defs/money",
            definition={"type": "integer"},
        )
        second = Occurrence(
            entry="a.json",
            pointer="/$defs/money",
            definition={"type": "integer"},
        )

        assert first == second
