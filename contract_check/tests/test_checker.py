"""Tests for the contract check, mirroring the release-note scenarios."""

import pytest
from semver import Version

from contract_check.checker import (
    LENIENT,
    NEW_WRITE_OLD_READ,
    OLD_WRITE_NEW_READ,
    STRICT,
    Field,
    Schema,
    check_contract,
    promise_scope,
)


def make_schema(version, fields):
    return Schema(product="orders", version=Version.parse(version), fields=tuple(fields))


ID = Field(name="id", type="string", required=True)
STATUS_V1 = Field(name="status", type="enum", required=True, values=("active", "paused"))
STATUS_V2 = Field(
    name="status", type="enum", required=True, values=("active", "paused", "archived")
)


class TestPromiseScope:
    def test_minor_and_patch_promise_backward_compatibility(self):
        for old, new in [("1.3.0", "1.4.0"), ("1.3.0", "1.3.1")]:
            promise, scope = promise_scope(Version.parse(old), Version.parse(new))
            assert "backward compatible" in promise
            assert scope == {OLD_WRITE_NEW_READ, NEW_WRITE_OLD_READ}

    def test_major_promises_nothing(self):
        promise, scope = promise_scope(Version.parse("1.9.0"), Version.parse("2.0.0"))
        assert "none" in promise
        assert scope == frozenset()

    def test_regression_is_rejected(self):
        with pytest.raises(ValueError, match="greater"):
            promise_scope(Version.parse("1.4.0"), Version.parse("1.4.0"))


class TestEnumWidening:
    """Release notes say backward compatible; old readers reject the new enum."""

    old = make_schema("1.3.0", [ID, STATUS_V1])
    new = make_schema("1.4.0", [ID, STATUS_V2])

    def test_minor_bump_with_new_enum_value_is_broken(self):
        verdict = check_contract(self.old, self.new, reader_convention=STRICT)
        assert verdict.status == "broken"
        (violation,) = verdict.violations
        assert violation.direction == NEW_WRITE_OLD_READ
        assert violation.kind == "unknown_enum_value"
        assert violation.field == "status"

    def test_witness_is_the_shortest_legal_source_record(self):
        verdict = check_contract(self.old, self.new)
        (violation,) = verdict.violations
        assert violation.record == {"id": "", "status": "archived"}

    def test_lenient_readers_still_reject_unknown_enum_values(self):
        verdict = check_contract(self.old, self.new, reader_convention=LENIENT)
        assert verdict.status == "broken"

    def test_same_change_under_a_major_bump_promises_nothing(self):
        new_major = make_schema("2.0.0", [ID, STATUS_V2])
        verdict = check_contract(self.old, new_major)
        assert verdict.status == "no_promise"
        assert verdict.violations == ()


class TestOptionalFieldAdded:
    """One extra optional field: harmless for lenient readers, fatal for strict."""

    old = make_schema("2.0.0", [ID, Field(name="email", type="string", required=True)])
    new = make_schema(
        "2.1.0",
        [ID, Field(name="email", type="string", required=True),
         Field(name="nickname", type="string", required=False)],
    )

    def test_strict_reader_rejects_unknown_field(self):
        verdict = check_contract(self.old, self.new, reader_convention=STRICT)
        assert verdict.status == "broken"
        (violation,) = verdict.violations
        assert violation.direction == NEW_WRITE_OLD_READ
        assert violation.kind == "unknown_field"
        assert violation.record == {"id": "", "email": "", "nickname": ""}

    def test_lenient_reader_ignores_unknown_field(self):
        verdict = check_contract(self.old, self.new, reader_convention=LENIENT)
        assert verdict.status == "kept"
        assert verdict.violations == ()


class TestRequiredFieldChanges:
    def test_new_required_field_breaks_old_write_new_read(self):
        old = make_schema("1.0.0", [ID])
        new = make_schema("1.1.0", [ID, Field(name="tenant", type="string", required=True)])
        verdict = check_contract(old, new)
        assert verdict.status == "broken"
        kinds = {(v.direction, v.kind) for v in verdict.violations}
        assert (OLD_WRITE_NEW_READ, "missing_required_field") in kinds
        witness = next(v for v in verdict.violations if v.kind == "missing_required_field")
        assert witness.record == {"id": ""}

    def test_optional_to_required_breaks_old_write_new_read(self):
        old = make_schema("1.0.0", [ID, Field(name="note", type="string", required=False)])
        new = make_schema("1.1.0", [ID, Field(name="note", type="string", required=True)])
        verdict = check_contract(old, new)
        assert verdict.status == "broken"
        (violation,) = verdict.violations
        assert violation.direction == OLD_WRITE_NEW_READ
        assert violation.kind == "required_tightened"
        assert violation.record == {"id": ""}


class TestKeptPromise:
    def test_unchanged_schema_keeps_promise(self):
        old = make_schema("1.3.0", [ID, STATUS_V1])
        new = make_schema("1.3.1", [ID, STATUS_V1])
        verdict = check_contract(old, new)
        assert verdict.status == "kept"

    def test_enum_narrowing_breaks_old_write_new_read(self):
        old = make_schema("1.3.0", [ID, STATUS_V2])
        new = make_schema("1.4.0", [ID, STATUS_V1])
        verdict = check_contract(old, new)
        assert verdict.status == "broken"
        (violation,) = verdict.violations
        assert violation.direction == OLD_WRITE_NEW_READ
        assert violation.kind == "unknown_enum_value"
        assert violation.record == {"id": "", "status": "archived"}
