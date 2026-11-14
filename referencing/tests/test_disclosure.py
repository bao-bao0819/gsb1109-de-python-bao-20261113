import pytest

from referencing import Anchor, Registry, Resource, Specification, exceptions
from referencing.disclosure import (
    DisclosurePackage,
    build_disclosure_package,
)
from referencing.jsonschema import DRAFT202012

ID_AND_CHILDREN = Specification(
    name="id-and-children",
    id_of=lambda contents: contents.get("ID"),
    subresources_of=lambda contents: contents.get("children", []),
    anchors_in=lambda specification, contents: [
        Anchor(
            name=name,
            resource=specification.create_resource(contents=each),
        )
        for name, each in contents.get("anchors", {}).items()
    ],
    maybe_in_subresource=lambda segments, resolver, subresource: resolver,
)


def opaque_registry(pairs):
    return Registry().with_resources(
        [(uri, Resource.opaque(contents)) for uri, contents in pairs],
    )


def test_allowed_field_keeps_original_explanation():
    entry = "urn:report"
    document = {"revenue": 100, "cost": 42}
    registry = opaque_registry([(entry, document)])

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/revenue")],
    )

    expected = registry.resolver(base_uri=entry).lookup("#/revenue").contents
    assert package.field(entry, "/revenue").explanation == expected
    assert package.explain(entry + "#/revenue") == 100
    assert package.is_disclosable(package.field(entry, "/revenue"))


def test_forbidden_field_is_pruned_from_surrogate():
    entry = "urn:report"
    registry = opaque_registry([(entry, {"revenue": 100, "cost": 42})])

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/revenue")],
        forbidden=[(entry, "/cost")],
    )

    assert package.explain(entry + "#/revenue") == 100
    with pytest.raises(exceptions.Unresolvable):
        package.explain(entry + "#/cost")
    assert "cost" not in package.registry.contents(entry)


def test_forbidden_field_cannot_be_reached_via_alias():
    entry = "urn:report"
    document = {"revenue": 100, "cost": 42, "ledger_copy": 42}
    registry = opaque_registry([(entry, document)])

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/revenue")],
        forbidden=[(entry, "/cost")],
    )

    forbidden = package.forbidden_fields[0]
    assert entry + "#/ledger_copy" in forbidden.aliases
    with pytest.raises(exceptions.Unresolvable):
        package.explain(entry + "#/ledger_copy")


def test_allowed_field_lists_its_aliases():
    entry = "urn:report"
    document = {"revenue": 100, "revenue_copy": 100, "cost": 42}
    registry = opaque_registry([(entry, document)])

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/revenue")],
    )

    aliases = package.field(entry, "/revenue").aliases
    assert entry + "#/revenue" in aliases
    assert entry + "#/revenue_copy" in aliases
    assert package.explain(entry + "#/revenue") == 100
    # The surrogate serves only the listed field path; an undeclared duplicate
    # location is recorded as alias metadata but never materialized.
    with pytest.raises(exceptions.Unresolvable):
        package.explain(entry + "#/revenue_copy")
    assert package.registry.contents(entry) == {"revenue": 100}


def test_nested_and_array_fields():
    entry = "urn:report"
    document = {"rows": [{"v": 1}, {"v": 2}], "secret": {"deep": 7}}
    registry = opaque_registry([(entry, document)])

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/rows/0/v"), (entry, "/rows/1")],
        forbidden=[(entry, "/secret/deep")],
    )

    assert package.explain(entry + "#/rows/0/v") == 1
    assert package.explain(entry + "#/rows/1") == {"v": 2}
    with pytest.raises(exceptions.Unresolvable):
        package.explain(entry + "#/secret/deep")
    assert package.registry.contents(entry)["rows"] == [{"v": 1}, {"v": 2}]


def test_surrogates_are_opaque_and_do_not_endorse_the_contract():
    entry = "urn:report"
    registry = opaque_registry([(entry, {"revenue": 100, "cost": 42})])

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/revenue")],
        forbidden=[(entry, "/cost")],
    )

    surrogate = package.registry[entry]
    assert surrogate._specification is Specification.OPAQUE
    assert surrogate.id() is None
    assert list(surrogate.anchors()) == []
    assert list(surrogate.subresources()) == []
    assert len(package.registry) == 1
    with pytest.raises(exceptions.Unresolvable):
        package.explain("urn:never:published")


def test_anchor_alias_of_forbidden_field_is_blocked():
    entry = "urn:report"
    document = {"anchors": {"ledger": {"cost": 42}}, "revenue": 100}
    resource = ID_AND_CHILDREN.create_resource(document)
    registry = Registry().with_resource(entry, resource)

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/revenue")],
        forbidden=[(entry, "/anchors/ledger")],
    )

    forbidden = package.forbidden_fields[0]
    assert entry + "#ledger" in forbidden.aliases
    with pytest.raises(exceptions.Unresolvable):
        package.explain(entry + "#ledger")
    assert package.explain(entry + "#/revenue") == 100


def test_crawled_subresource_uri_alias_is_blocked():
    entry = "urn:report"
    child = {"ID": "urn:shared/ledger", "cost": 42}
    root = ID_AND_CHILDREN.create_resource(
        {"ID": entry, "children": [child], "revenue": 100},
    )
    registry = root @ Registry()

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/revenue")],
        forbidden=[(entry, "/children/0")],
    )

    forbidden = package.forbidden_fields[0]
    assert "urn:shared/ledger" in forbidden.aliases
    with pytest.raises(exceptions.Unresolvable):
        package.explain("urn:shared/ledger")
    assert "urn:shared/ledger" not in package.registry


def test_shared_subresource_conflicts_across_entry_points():
    entry_a = "urn:org-a/report"
    entry_b = "urn:org-b/report"
    shared = {"ID": "urn:shared/ledger", "revenue": 100, "cost": 42}
    root_a = ID_AND_CHILDREN.create_resource(
        {"ID": entry_a, "children": [dict(shared)]},
    )
    root_b = ID_AND_CHILDREN.create_resource(
        {"ID": entry_b, "children": [dict(shared)]},
    )
    registry = [root_a, root_b] @ Registry()

    package = build_disclosure_package(
        registry,
        allowed=[(entry_a, "/children/0/revenue")],
        forbidden=[(entry_b, "/children/0")],
    )

    assert len(package.conflicts) == 1
    conflict = package.conflicts[0]
    assert set(conflict.entries) == {entry_a, entry_b}
    assert conflict.fields[0].entry == entry_a
    assert conflict.fields[1].entry == entry_b
    assert entry_a + "#/children/0" in conflict.shared_aliases
    assert not package.is_disclosable(conflict.fields[0])


def test_parent_child_fields_conflict_within_one_entry():
    entry = "urn:report"
    registry = opaque_registry(
        [(entry, {"outer": {"allowed": 1, "forbidden": 2}})],
    )

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/outer/allowed")],
        forbidden=[(entry, "/outer")],
    )

    assert len(package.conflicts) == 1
    conflict = package.conflicts[0]
    assert conflict.entries == (entry,)
    assert not package.is_disclosable(conflict.fields[0])


def test_disjoint_fields_have_no_conflicts():
    entry = "urn:report"
    registry = opaque_registry([(entry, {"revenue": 100, "cost": 42})])

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/revenue")],
        forbidden=[(entry, "/cost")],
    )

    assert package.conflicts == ()
    assert package.is_disclosable(package.field(entry, "/revenue"))


def test_json_schema_resources_are_repackaged_as_opaque():
    entry = "urn:schema"
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": entry,
        "title": "report",
        "cost": 42,
    }
    registry = Registry().with_contents([(entry, schema)])

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/title")],
        forbidden=[(entry, "/cost")],
    )

    assert package.registry[entry]._specification is Specification.OPAQUE
    assert package.explain(entry + "#/title") == "report"
    with pytest.raises(exceptions.Unresolvable):
        package.explain(entry + "#/cost")
    assert DRAFT202012  # the original dialect is deliberately not carried over


def test_overlapping_allowed_and_forbidden_rejected():
    entry = "urn:report"
    registry = opaque_registry([(entry, {"revenue": 100})])

    with pytest.raises(ValueError, match="both allowed and forbidden"):
        build_disclosure_package(
            registry,
            allowed=[(entry, "/revenue")],
            forbidden=[(entry, "/revenue")],
        )


def test_invalid_pointer_rejected():
    registry = opaque_registry([("urn:report", {"revenue": 100})])

    with pytest.raises(ValueError, match="must start with '/'"):
        build_disclosure_package(
            registry,
            allowed=[("urn:report", "revenue")],
        )


def test_unknown_field_lookup_raises():
    registry = opaque_registry([("urn:report", {"revenue": 100})])
    package = build_disclosure_package(
        registry,
        allowed=[("urn:report", "/revenue")],
    )

    with pytest.raises(KeyError):
        package.field("urn:report", "/missing")


def test_duplicate_field_declarations_are_deduped():
    entry = "urn:report"
    registry = opaque_registry([(entry, {"revenue": 100, "cost": 42})])

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/revenue"), (entry + "#", "/revenue")],
        forbidden=[(entry, "/cost")],
    )

    assert len(package.allowed_fields) == 1
    assert len(package.forbidden_fields) == 1
    assert isinstance(package, DisclosurePackage)


def test_separable_leaves_in_shared_subresource_need_not_conflict():
    entry_a = "urn:org-a/report"
    entry_b = "urn:org-b/report"
    shared = {"revenue": 100, "cost": 42}
    root_a = ID_AND_CHILDREN.create_resource(
        {"ID": entry_a, "children": [dict(shared)]},
    )
    root_b = ID_AND_CHILDREN.create_resource(
        {"ID": entry_b, "children": [dict(shared)]},
    )
    registry = [root_a, root_b] @ Registry()

    package = build_disclosure_package(
        registry,
        allowed=[(entry_a, "/children/0/revenue")],
        forbidden=[(entry_b, "/children/0/cost")],
    )

    assert package.conflicts == ()
    assert package.explain(entry_a + "#/children/0/revenue") == 100
    with pytest.raises(exceptions.Unresolvable):
        package.explain(entry_a + "#/children/0/cost")
    with pytest.raises(exceptions.Unresolvable):
        package.explain(entry_b + "#/children/0/cost")


def test_equal_value_across_entries_is_coupled_as_a_conflict():
    entry_a = "urn:org-a/report"
    entry_b = "urn:org-b/report"
    registry = opaque_registry(
        [(entry_a, {"figure": 99}), (entry_b, {"figure": 99})],
    )

    package = build_disclosure_package(
        registry,
        allowed=[(entry_a, "/figure")],
        forbidden=[(entry_b, "/figure")],
    )

    assert len(package.conflicts) == 1
    conflict = package.conflicts[0]
    assert set(conflict.entries) == {entry_a, entry_b}
    with pytest.raises(exceptions.Unresolvable):
        package.explain(entry_a + "#/figure")


def test_escaped_json_pointer_characters_round_trip():
    entry = "urn:report"
    document = {"a/b": {"c~d": 7}, "secret": 1}
    registry = opaque_registry([(entry, document)])

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/a~1b/c~0d")],
        forbidden=[(entry, "/secret")],
    )

    assert package.explain(entry + "#/a~1b/c~0d") == 7
    with pytest.raises(exceptions.Unresolvable):
        package.explain(entry + "#/secret")
    assert package.registry.contents(entry) == {"a/b": {"c~d": 7}}


def test_original_registry_is_not_mutated():
    entry = "urn:report"
    document = {"revenue": 100, "cost": {"deep": 42}}
    registry = opaque_registry([(entry, document)])

    package = build_disclosure_package(
        registry,
        allowed=[(entry, "/revenue")],
        forbidden=[(entry, "/cost/deep")],
    )

    assert registry.crawl().contents(entry) == document
    assert package.registry.contents(entry) == {"revenue": 100}
