import pytest

from referencing import Registry, Resource
from referencing.migration import (
    ADDED,
    CHANGED,
    REMOVED,
    UNDECIDED,
    ComparisonReport,
    EquivalenceCriteria,
    FieldChange,
    FieldDefinition,
    compare_versions,
)

ENTRY = "urn:contract:order"


def schema(*properties, required=(), **named):
    fields = dict(named)
    fields.update(dict(properties))
    document = {"type": "object", "properties": fields}
    if required:
        document["required"] = list(required)
    return document


def test_relocated_but_equivalent_is_not_a_change():
    old = {
        ENTRY: schema(
            amount={
                "type": "number",
                "unit": "yuan",
                "description": "old spot",
            },
        ),
    }
    new = {
        ENTRY: schema(
            wrapper={
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "number",
                        "unit": "yuan",
                        "description": "new spot",
                    },
                },
            },
        ),
    }

    report = compare_versions(
        old,
        new,
        [ENTRY],
        used_fields={ENTRY: ["amount"]},
    )

    assert report.definite == ()
    assert report.undecided == ()
    ((old_definition, new_definition),) = report.equivalent
    assert old_definition.name == "amount"
    assert old_definition.location == "/properties/amount"
    assert new_definition.location == (
        "/properties/wrapper/properties/amount"
    )
    assert old_definition.definition == {
        "type": "number",
        "unit": "yuan",
        "description": "old spot",
    }
    assert new_definition.definition["unit"] == "yuan"

    full_report = compare_versions(old, new, [ENTRY])
    (wrapper_change,) = full_report.definite
    assert wrapper_change.kind == ADDED
    assert wrapper_change.field == "wrapper"


def test_same_name_unit_change_is_definite():
    old = {ENTRY: schema(price={"type": "number", "unit": "yuan"})}
    new = {ENTRY: schema(price={"type": "number", "unit": "cent"})}

    report = compare_versions(old, new, [ENTRY])

    (change,) = report
    assert change.kind == CHANGED
    assert change.field == "price"
    assert change.differences == ("unit",)
    assert "单位" in change.reason
    assert change.locations == (
        "/properties/price",
        "/properties/price",
    )
    assert isinstance(change.old, FieldDefinition)
    assert isinstance(change.new, FieldDefinition)
    assert change.old.definition["unit"] == "yuan"
    assert change.new.definition["unit"] == "cent"


def test_same_name_enum_change_is_definite():
    old = {
        ENTRY: schema(
            status={"type": "string", "enum": ["on", "off"]},
        ),
    }
    new = {
        ENTRY: schema(
            status={"type": "string", "enum": ["on", "off", "paused"]},
        ),
    }

    report = compare_versions(old, new, [ENTRY])

    (change,) = report
    assert change.kind == CHANGED
    assert change.differences == ("enum",)
    assert "枚举" in change.reason


def test_missing_value_convention_changes_are_definite():
    old_default = {
        ENTRY: schema(
            flag={"type": "boolean", "default": False},
        ),
    }
    new_default = {ENTRY: schema(flag={"type": "boolean"})}
    report = compare_versions(old_default, new_default, [ENTRY])
    (change,) = report
    assert change.kind == CHANGED
    assert change.differences == ("default:present", "default:value")
    assert "缺值约定" in change.reason

    old_nullable = {
        ENTRY: schema(note={"type": ["string", "null"]}),
    }
    new_nullable = {ENTRY: schema(note={"type": "string"})}
    report = compare_versions(old_nullable, new_nullable, [ENTRY])
    (change,) = report
    assert change.kind == CHANGED
    assert set(change.differences) == {"nullable", "type"}


def test_required_convention_change_is_definite():
    old = {ENTRY: schema(note={"type": "string"}, required=["note"])}
    new = {ENTRY: schema(note={"type": "string"})}

    report = compare_versions(old, new, [ENTRY])

    (change,) = report
    assert change.kind == CHANGED
    assert change.differences == ("required",)


def test_difference_outside_criteria_is_undecided():
    old = {
        ENTRY: schema(
            amount={"type": "number", "x-precision": "0.01"},
        ),
    }
    new = {
        ENTRY: schema(
            amount={"type": "number", "x-precision": "0.0001"},
        ),
    }

    report = compare_versions(old, new, [ENTRY])

    (change,) = report
    assert change.kind == UNDECIDED
    assert change.undecided
    assert change.differences == ("x-precision",)
    assert "x-precision" in change.reason
    assert report.definite == ()
    assert len(report.undecided) == 1


def test_business_can_extend_covered_properties():
    old = {
        ENTRY: schema(
            amount={"type": "number", "x-precision": "0.01"},
        ),
    }
    new = {
        ENTRY: schema(
            amount={"type": "number", "x-precision": "0.0001"},
        ),
    }
    criteria = EquivalenceCriteria(
        covered_properties=frozenset(
            {"type", "enum", "const", "unit", "x-precision"},
        ),
    )

    report = compare_versions(old, new, [ENTRY], criteria)

    (change,) = report
    assert change.kind == CHANGED
    assert change.differences == ("x-precision",)


def test_property_alias_feeds_normalized_convention():
    old = {
        ENTRY: schema(
            amount={"type": "number", "x-unit": "yuan"},
        ),
    }
    new = {
        ENTRY: schema(
            amount={"type": "number", "x-unit": "cent"},
        ),
    }
    criteria = EquivalenceCriteria(property_aliases={"x-unit": "unit"})

    report = compare_versions(old, new, [ENTRY], criteria)

    (change,) = report
    assert change.kind == CHANGED
    assert change.differences == ("unit",)


def test_custom_equivalence_callback_overrides():
    old = {
        ENTRY: schema(
            amount={"type": "number", "x-precision": "0.01"},
        ),
    }
    new = {
        ENTRY: schema(
            amount={"type": "number", "x-precision": "0.0001"},
        ),
    }

    forced_equal = EquivalenceCriteria(
        field_equivalence=lambda old_def, new_def: True,
    )
    report = compare_versions(old, new, [ENTRY], forced_equal)
    assert tuple(report) == ()
    assert len(report.equivalent) == 1

    forced_change = EquivalenceCriteria(
        field_equivalence=lambda old_def, new_def: False,
    )
    report = compare_versions(old, new, [ENTRY], forced_change)
    (change,) = report
    assert change.kind == CHANGED

    deferred = EquivalenceCriteria(
        field_equivalence=lambda old_def, new_def: None,
    )
    report = compare_versions(old, new, [ENTRY], deferred)
    (change,) = report
    assert change.kind == UNDECIDED


def test_added_and_removed_fields():
    old = {ENTRY: schema(kept={"type": "string"}, gone={"type": "integer"})}
    new = {ENTRY: schema(kept={"type": "string"}, fresh={"type": "integer"})}

    report = compare_versions(old, new, [ENTRY])

    kinds = {(change.field, change.kind) for change in report}
    assert kinds == {("gone", REMOVED), ("fresh", ADDED)}
    removed = next(change for change in report if change.kind == REMOVED)
    assert removed.new is None
    assert removed.old is not None
    assert removed.locations[0] is not None
    assert removed.locations[1] is None
    added = next(change for change in report if change.kind == ADDED)
    assert added.old is None
    assert added.new is not None
    assert added.locations[0] is None
    assert added.locations[1] is not None
    assert "gone" in str(report)
    assert report.equivalent[0][0].name == "kept"


def test_used_fields_limits_the_minimal_list():
    old = {
        ENTRY: schema(
            used={"type": "string"},
            ignored={"type": "string", "enum": ["a"]},
        ),
    }
    new = {
        ENTRY: schema(
            used={"type": "string"},
            ignored={"type": "string", "enum": ["a", "b"]},
        ),
    }

    report = compare_versions(
        old,
        new,
        [ENTRY],
        used_fields={ENTRY: ["used"]},
    )

    assert tuple(report) == ()
    assert [pair[0].name for pair in report.equivalent] == ["used"]


def test_entry_missing_in_one_version():
    old = {ENTRY: schema(amount={"type": "number"})}
    new = {"urn:other": schema(amount={"type": "integer"})}

    removed_report = compare_versions(old, new, [ENTRY])
    (change,) = removed_report
    assert change.kind == REMOVED

    added_report = compare_versions(new, old, [ENTRY])
    (change,) = added_report
    assert change.kind == ADDED


def test_entry_unreachable_in_both_versions_raises():
    with pytest.raises(ValueError, match="unreachable"):
        compare_versions({}, {}, [ENTRY])


def test_registry_entry_with_pointer_fragment_is_traceable():
    document = schema(amount={"type": "number", "unit": "yuan"})
    registry = Registry().with_resource(
        ENTRY,
        Resource.opaque(contents=document),
    )
    new_document = schema(amount={"type": "number", "unit": "cent"})
    new_registry = Registry().with_resource(
        ENTRY,
        Resource.opaque(contents=new_document),
    )

    report = compare_versions(
        registry,
        new_registry,
        [f"{ENTRY}#/properties/amount"],
    )

    (change,) = report
    assert change.kind == CHANGED
    assert change.entry == f"{ENTRY}#/properties/amount"
    assert change.field == "amount"
    assert change.locations == ("", "")
    assert change.old.definition["unit"] == "yuan"
    assert change.new.definition["unit"] == "cent"


def test_nested_array_items_are_walked():
    document = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "enum": ["x"]},
                    },
                },
            },
        },
    }
    old = {ENTRY: document}
    new = {
        ENTRY: {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "enum": ["x", "y"]},
                        },
                    },
                },
            },
        },
    }

    report = compare_versions(old, new, [ENTRY])

    (change,) = report
    assert change.field == "code"
    assert change.old.location == (
        "/properties/items/items/properties/code"
    )


def test_boolean_and_non_mapping_schemas_are_skipped():
    old = {
        ENTRY: {
            "type": "object",
            "properties": {"loose": True, "named": {"type": "string"}},
        },
    }
    new = {
        ENTRY: {
            "type": "object",
            "properties": {"loose": False, "named": {"type": "string"}},
        },
    }

    report = compare_versions(old, new, [ENTRY])

    assert tuple(report) == ()


def test_non_mapping_root_produces_no_fields():
    registry = Registry().with_resource(
        ENTRY,
        Resource.opaque(contents=["not", "an", "object"]),
    )
    report = compare_versions(registry, registry, [ENTRY])
    assert isinstance(report, ComparisonReport)
    assert tuple(report) == ()


def test_report_str_and_change_locations():
    old = {ENTRY: schema(price={"type": "number", "unit": "yuan"})}
    new = {ENTRY: schema(price={"type": "number", "unit": "cent"})}

    report = compare_versions(old, new, [ENTRY])
    text = str(report)
    assert CHANGED in text
    assert ENTRY in text
    assert "price" in text
    assert len(report) == 1

    (change,) = report
    assert isinstance(change, FieldChange)
    assert change.locations[0] == change.old.location
    assert change.locations[1] == change.new.location


def test_const_and_default_value_changes_are_definite():
    old = {
        ENTRY: schema(
            kind={"const": "v1"},
            counted={"type": "integer", "default": 0},
        ),
    }
    new = {
        ENTRY: schema(
            kind={"const": "v2"},
            counted={"type": "integer", "default": 1},
        ),
    }

    report = compare_versions(old, new, [ENTRY])

    differences = {
        change.field: change.differences for change in report
    }
    assert differences["kind"] == ("const",)
    assert differences["counted"] == ("default:value",)


def test_non_list_required_is_tolerated():
    broken = {
        ENTRY: {
            "type": "object",
            "required": "note",
            "properties": {"note": {"type": "string"}},
        },
    }
    report = compare_versions(broken, broken, [ENTRY])
    assert tuple(report) == ()


def test_duplicate_field_names_pair_by_smallest_distance():
    old = {
        ENTRY: {
            "type": "object",
            "properties": {
                "group": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number", "unit": "yuan"},
                    },
                },
                "other": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number", "unit": "yuan"},
                    },
                },
            },
        },
    }
    new = {
        ENTRY: {
            "type": "object",
            "properties": {
                "group": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number", "unit": "yuan"},
                    },
                },
                "other": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number", "unit": "cent"},
                    },
                },
            },
        },
    }

    report = compare_versions(old, new, [ENTRY])

    (change,) = report
    assert change.kind == CHANGED
    assert change.old.location == (
        "/properties/other/properties/amount"
    )
    assert change.new.location == (
        "/properties/other/properties/amount"
    )
    equivalent_names = [
        (pair[0].location, pair[1].location)
        for pair in report.equivalent
        if pair[0].name == "amount"
    ]
    assert equivalent_names == [
        (
            "/properties/group/properties/amount",
            "/properties/group/properties/amount",
        ),
    ]


def test_duplicate_field_names_can_relocate_across_locations():
    def document(group_unit, other_unit):
        return {
            "type": "object",
            "properties": {
                "group": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number", "unit": group_unit},
                    },
                },
                "other": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number", "unit": other_unit},
                    },
                },
            },
        }

    report = compare_versions(
        {ENTRY: document("yuan", "cent")},
        {ENTRY: document("cent", "yuan")},
        [ENTRY],
    )

    assert tuple(report) == ()
    relocated = {
        (pair[0].location, pair[1].location)
        for pair in report.equivalent
        if pair[0].name == "amount"
    }
    assert len(relocated) == 2
    assert (
        "/properties/group/properties/amount",
        "/properties/other/properties/amount",
    ) in relocated
    assert (
        "/properties/other/properties/amount",
        "/properties/group/properties/amount",
    ) in relocated


def test_nullable_marker_without_type_is_normalized():
    old = {
        ENTRY: schema(note={"x-nullable": True}),
    }
    new = {
        ENTRY: schema(note={"x-nullable": False}),
    }

    report = compare_versions(old, new, [ENTRY])

    (change,) = report
    assert change.kind == CHANGED
    assert change.differences == ("nullable",)


def test_boolean_items_schema_is_skipped():
    document = {
        "type": "object",
        "properties": {
            "things": {"type": "array", "items": True},
            "named": {"type": "string"},
        },
    }
    report = compare_versions(
        {ENTRY: document},
        {ENTRY: document},
        [ENTRY],
    )
    assert [pair[0].name for pair in report.equivalent] == [
        "named",
        "things",
    ]


def test_used_fields_without_entry_key_covers_every_field():
    old = {
        ENTRY: schema(
            one={"type": "string"},
            two={"type": "string", "enum": ["a"]},
        ),
    }
    new = {
        ENTRY: schema(
            one={"type": "string"},
            two={"type": "string", "enum": ["a", "b"]},
        ),
    }

    report = compare_versions(
        old,
        new,
        [ENTRY],
        used_fields={"urn:other": ["one"]},
    )

    (change,) = report
    assert change.field == "two"


def test_same_named_field_count_mismatch_leaves_a_removal():
    old = {
        ENTRY: {
            "type": "object",
            "properties": {
                "group": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number", "unit": "yuan"},
                    },
                },
                "other": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number", "unit": "yuan"},
                    },
                },
            },
        },
    }
    new = {
        ENTRY: {
            "type": "object",
            "properties": {
                "group": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number", "unit": "yuan"},
                    },
                },
            },
        },
    }

    report = compare_versions(old, new, [ENTRY])

    removals = [
        change
        for change in report
        if change.kind == REMOVED and change.field == "amount"
    ]
    (removal,) = removals
    assert removal.old.location == (
        "/properties/other/properties/amount"
    )


def test_missing_entry_respects_used_fields_filter():
    old = {
        ENTRY: schema(
            used={"type": "string"},
            ignored={"type": "string"},
        ),
    }

    report = compare_versions(
        old,
        {"urn:other": schema(used={"type": "string"})},
        [ENTRY],
        used_fields={ENTRY: ["used"]},
    )

    (change,) = report
    assert change.kind == REMOVED
    assert change.field == "used"
