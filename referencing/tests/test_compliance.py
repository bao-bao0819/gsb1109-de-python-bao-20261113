import pytest

from referencing import Registry, exceptions
from referencing.compliance import (
    DependencyStatus,
    analyze,
    redirect,
)
from referencing.jsonschema import DRAFT202012

WITHDRAWN = "http://example.com/withdrawn"
MIRROR = "http://example.com/mirror"
OTHER = "http://example.com/other"


def resource(contents):
    return DRAFT202012.create_resource(contents)


def registry_with(mapping):
    return Registry().with_contents(
        mapping.items(),
        default_specification=DRAFT202012,
    )


class TestAnalyze:
    def test_unrelated_entrypoint_never_reaches_the_contract(self):
        registry = registry_with(
            {
                "http://example.com/entry": {
                    "$ref": OTHER,
                },
                OTHER: {},
            },
        )
        report = analyze(registry, withdrawn=WITHDRAWN)

        impact = report.impact_for("http://example.com/entry")
        assert impact.status is DependencyStatus.UNRELATED
        assert impact.references == ()
        assert report.requires_revalidation() == ()

    def test_direct_dependent_entrypoint(self):
        registry = registry_with(
            {"http://example.com/entry": {"$ref": WITHDRAWN}},
        )
        report = analyze(registry, withdrawn=WITHDRAWN)

        impact = report.impact_for("http://example.com/entry")
        assert impact.status is DependencyStatus.DEPENDENT
        (reference,) = impact.references
        assert reference.source == "http://example.com/entry"
        assert reference.target == WITHDRAWN
        assert reference.pointer == "/$ref"
        assert reference.ref == WITHDRAWN
        assert reference.via == ("http://example.com/entry",)

    def test_transitive_dependency_records_the_full_chain(self):
        registry = registry_with(
            {
                "http://example.com/entry": {
                    "$ref": "http://example.com/intermediate",
                },
                "http://example.com/intermediate": {"$ref": WITHDRAWN},
            },
        )
        report = analyze(registry, withdrawn=WITHDRAWN)

        impact = report.impact_for("http://example.com/entry")
        assert impact.status is DependencyStatus.DEPENDENT
        (reference,) = impact.references
        assert reference.source == "http://example.com/intermediate"
        assert reference.target == WITHDRAWN
        assert reference.via == (
            "http://example.com/entry",
            "http://example.com/intermediate",
        )

    def test_nested_reference_pointer_and_fragment_are_traced(self):
        registry = registry_with(
            {
                "http://example.com/entry": {
                    "properties": {
                        "name": {"$ref": WITHDRAWN + "#/definitions/Name"},
                    },
                },
            },
        )
        report = analyze(registry, withdrawn=WITHDRAWN)

        (reference,) = report.impact_for(
            "http://example.com/entry",
        ).references
        assert reference.pointer == "/properties/name/$ref"
        assert reference.fragment == "/definitions/Name"
        assert reference.target == WITHDRAWN

    def test_base_uri_changes_inside_nested_subresources(self):
        registry = registry_with(
            {
                "http://example.com/entry": {
                    "$id": "http://example.com/entry",
                    "$defs": {
                        "scoped": {
                            "$id": "sub/",
                            "$ref": "../withdrawn",
                        },
                    },
                },
            },
        )
        report = analyze(registry, withdrawn=WITHDRAWN)

        impact = report.impact_for("http://example.com/entry")
        assert impact.status is DependencyStatus.DEPENDENT
        (reference,) = impact.references
        assert reference.target == WITHDRAWN
        assert reference.pointer == "/$defs/scoped/$ref"

    def test_hash_only_id_does_not_change_the_base_uri(self):
        registry = registry_with(
            {
                "http://example.com/entry": {
                    "$defs": {
                        "local": {
                            "$id": "#localAnchor",
                            "$ref": WITHDRAWN,
                        },
                    },
                },
            },
        )
        report = analyze(registry, withdrawn=WITHDRAWN)

        (reference,) = report.impact_for(
            "http://example.com/entry",
        ).references
        assert reference.target == WITHDRAWN

    def test_mirror_only_entrypoint_is_mirrorable_without_ingestion(self):
        registry = registry_with(
            {"http://example.com/entry": {"$ref": MIRROR}},
        )
        report = analyze(registry, withdrawn=WITHDRAWN, mirrors=[MIRROR])

        impact = report.impact_for("http://example.com/entry")
        assert impact.status is DependencyStatus.MIRRORABLE
        assert impact.references[0].target == MIRROR

    def test_withdrawn_path_is_mirrorable_with_an_ingested_mirror(self):
        registry = registry_with(
            {
                "http://example.com/entry": {"$ref": WITHDRAWN},
                MIRROR: {"mirrored": True},
            },
        )
        report = analyze(registry, withdrawn=WITHDRAWN, mirrors=[MIRROR])

        assert (
            report.impact_for("http://example.com/entry").status
            is DependencyStatus.MIRRORABLE
        )

    def test_declared_mirror_not_ingested_cannot_substitute(self):
        registry = registry_with(
            {"http://example.com/entry": {"$ref": WITHDRAWN}},
        )
        report = analyze(registry, withdrawn=WITHDRAWN, mirrors=[MIRROR])

        assert (
            report.impact_for("http://example.com/entry").status
            is DependencyStatus.DEPENDENT
        )

    def test_reference_cycles_terminate_and_remain_unrelated(self):
        registry = registry_with(
            {
                "http://example.com/a": {"$ref": "http://example.com/b"},
                "http://example.com/b": {"$ref": "http://example.com/a"},
            },
        )
        report = analyze(registry, withdrawn=WITHDRAWN)

        assert report.entrypoints(DependencyStatus.UNRELATED) == (
            "http://example.com/a",
            "http://example.com/b",
        )

    def test_cycle_that_reaches_the_withdrawn_contract(self):
        registry = registry_with(
            {
                "http://example.com/a": {"$ref": "http://example.com/b"},
                "http://example.com/b": {
                    "allOf": [
                        {"$ref": "http://example.com/a"},
                        {"$ref": WITHDRAWN},
                    ],
                },
            },
        )
        report = analyze(registry, withdrawn=WITHDRAWN)

        assert report.entrypoints(DependencyStatus.DEPENDENT) == (
            "http://example.com/a",
            "http://example.com/b",
        )
        impact = report.impact_for("http://example.com/a")
        assert impact.references[0].pointer == "/allOf/1/$ref"

    def test_dynamic_reference_keywords_are_traced(self):
        registry = registry_with(
            {
                "http://example.com/entry": {
                    "$dynamicAnchor": "thing",
                    "$dynamicRef": WITHDRAWN + "#thing",
                },
            },
        )
        report = analyze(registry, withdrawn=WITHDRAWN)

        (reference,) = report.impact_for(
            "http://example.com/entry",
        ).references
        assert reference.ref == WITHDRAWN + "#thing"
        assert reference.fragment == "thing"
        assert reference.pointer == "/$dynamicRef"

    def test_only_the_single_withdrawn_uri_is_watched(self):
        registry = registry_with(
            {
                "http://example.com/entry": {
                    "allOf": [
                        {"$ref": OTHER},
                        {"$ref": WITHDRAWN},
                    ],
                },
                OTHER: {},
            },
        )
        report = analyze(registry, withdrawn=WITHDRAWN)

        targets = {
            each.target
            for each in report.impact_for(
                "http://example.com/entry",
            ).references
        }
        assert targets == {WITHDRAWN}

    def test_crawled_subresources_are_not_new_entrypoints(self):
        root = resource(
            {
                "$id": "http://example.com/root",
                "$defs": {
                    "child": {
                        "$id": "child",
                        "$ref": WITHDRAWN,
                    },
                },
            },
        )
        registry = root @ Registry()
        report = analyze(registry, withdrawn=WITHDRAWN)

        assert tuple(report.impacts) == ("http://example.com/root",)
        assert (
            report.impact_for("http://example.com/root").status
            is DependencyStatus.DEPENDENT
        )

    def test_requires_revalidation_lists_every_impacted_entrypoint(self):
        registry = registry_with(
            {
                "http://example.com/dependent": {"$ref": WITHDRAWN},
                "http://example.com/mirrorable": {"$ref": MIRROR},
                "http://example.com/unrelated": {"$ref": OTHER},
                MIRROR: {},
                OTHER: {},
            },
        )
        report = analyze(registry, withdrawn=WITHDRAWN, mirrors=[MIRROR])

        assert report.requires_revalidation() == (
            "http://example.com/dependent",
            "http://example.com/mirrorable",
        )
        unrelated = report.entrypoints(DependencyStatus.UNRELATED)
        assert "http://example.com/unrelated" in unrelated
        assert not set(report.requires_revalidation()) & {
            MIRROR,
            OTHER,
            "http://example.com/unrelated",
        }

    def test_impact_for_unknown_entrypoint(self):
        report = analyze(Registry(), withdrawn=WITHDRAWN)
        with pytest.raises(exceptions.NoSuchResource):
            report.impact_for("http://example.com/nope")


class TestRedirect:
    def test_withdrawn_uri_is_served_by_the_mirror(self):
        mirror_resource = resource({"mirrored": True})
        registry = Registry().with_resource(MIRROR, mirror_resource)

        redirected = redirect(registry, withdrawn=WITHDRAWN, mirror=MIRROR)

        assert redirected[WITHDRAWN] == mirror_resource
        with pytest.raises(exceptions.NoSuchResource):
            registry[WITHDRAWN]

    def test_redirect_requires_an_ingested_mirror(self):
        with pytest.raises(exceptions.NoSuchResource):
            redirect(Registry(), withdrawn=WITHDRAWN, mirror=MIRROR)

    def test_redirected_registry_resolves_the_withdrawn_reference(self):
        registry = registry_with(
            {
                "http://example.com/entry": {"$ref": WITHDRAWN},
                MIRROR: {"mirrored": True},
            },
        )
        redirected = redirect(registry, withdrawn=WITHDRAWN, mirror=MIRROR)

        resolved = redirected.resolver().lookup(WITHDRAWN)
        assert resolved.contents == {"mirrored": True}
