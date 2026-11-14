"""Planning of release tags across parallel release lines."""

import pytest

from semver import (
    ReleaseCandidate,
    ReleaseConstraintsError,
    ReleaseLine,
    ReleaseRegistry,
    Version,
    plan_releases,
)


def parse(versions):
    return [Version.parse(version) for version in versions]


def plan_map(result):
    return {candidate.line: candidate.planned for candidate in result}


def test_two_lines_plan_from_their_own_bases():
    legacy = ReleaseLine("legacy-2.x", "2.4.7", priority=10)
    feature = ReleaseLine("feature-3.x", "3.0.0-rc.2", priority=20)

    result = plan_releases([legacy, feature])

    assert plan_map(result) == {
        "feature-3.x": Version.parse("3.0.0-rc.3"),
        "legacy-2.x": Version.parse("2.4.8-rc.1"),
    }
    assert [candidate.line for candidate in result] == [
        "feature-3.x",
        "legacy-2.x",
    ]
    assert all(isinstance(candidate, ReleaseCandidate) for candidate in result)


def test_higher_priority_line_claims_lowest_free_tag_first():
    urgent = ReleaseLine("urgent", "1.0.0", priority=100)
    normal = ReleaseLine("normal", "1.0.0", priority=1)

    result = plan_releases([normal, urgent])

    assert plan_map(result)["urgent"] == Version.parse("1.0.1-rc.1")
    assert plan_map(result)["normal"] == Version.parse("1.0.1-rc.2")


def test_priority_tie_is_broken_by_line_name():
    first = ReleaseLine("alpha-line", "1.0.0")
    second = ReleaseLine("beta-line", "1.0.0")

    result = plan_releases([second, first])

    assert [candidate.line for candidate in result] == ["alpha-line", "beta-line"]
    assert plan_map(result)["alpha-line"] == Version.parse("1.0.1-rc.1")
    assert plan_map(result)["beta-line"] == Version.parse("1.0.1-rc.2")


def test_published_number_is_never_reassigned_even_when_withdrawn():
    line = ReleaseLine("main", "1.0.0")
    registry = ReleaseRegistry(used=parse(["1.0.1-rc.1", "1.0.1-rc.2"]))

    result = plan_releases([line], registry=registry)

    assert plan_map(result)["main"] == Version.parse("1.0.1-rc.3")


def test_reserved_and_revoked_numbers_are_skipped_like_used_ones():
    line = ReleaseLine("main", "1.0.0")
    registry = ReleaseRegistry(
        used=parse(["1.0.1-rc.1"]),
        blocked=parse(["1.0.1-rc.2", "1.0.1-rc.3"]),
    )

    result = plan_releases([line], registry=registry)

    assert plan_map(result)["main"] == Version.parse("1.0.1-rc.4")


def test_published_final_release_seals_the_whole_core_version():
    line = ReleaseLine("main", "1.0.0")
    registry = ReleaseRegistry(
        used=parse(["1.0.1"]),
        blocked=parse(["1.0.1-rc.5"]),
    )

    result = plan_releases([line], registry=registry)

    planned = plan_map(result)["main"]
    assert planned == Version.parse("1.0.2-rc.1")
    assert planned > Version.parse("1.0.1")


def test_revoked_but_unpublished_final_does_not_seal_core_version():
    line = ReleaseLine("main", "1.0.0")
    registry = ReleaseRegistry(blocked=parse(["1.0.1", "1.0.1-rc.1"]))

    result = plan_releases([line], registry=registry)

    assert plan_map(result)["main"] == Version.parse("1.0.1-rc.2")


def test_build_metadata_never_raises_priority_and_is_never_emitted():
    line = ReleaseLine("main", "1.0.1-rc.0+build.42")
    registry = ReleaseRegistry(
        used=parse(["1.0.1-rc.1+exp.sha.5114f85"]),
        blocked=parse(["1.0.1-rc.2+build.9"]),
    )

    result = plan_releases([line], registry=registry)

    planned = plan_map(result)["main"]
    assert planned.build is None
    assert planned == Version.parse("1.0.1-rc.3")
    assert Version.parse("1.0.1-rc.1+anything") in registry.consumed


def test_dependency_forces_candidate_to_be_strictly_later():
    earlier = ReleaseLine("earlier", "3.0.0-rc.2", priority=20)
    later = ReleaseLine("later", "3.0.0-rc.1", priority=10)

    result = plan_releases(
        [later, earlier],
        dependencies=[("later", "earlier")],
    )

    planned = plan_map(result)
    assert planned["earlier"] == Version.parse("3.0.0-rc.3")
    assert planned["later"] == Version.parse("3.0.0-rc.4")
    assert planned["later"] > planned["earlier"]


def test_dependency_across_major_lines_cannot_be_bridged():
    earlier = ReleaseLine("feature-3.x", "3.0.0-rc.2", priority=20)
    later = ReleaseLine("legacy-2.x", "2.4.7", priority=10)

    with pytest.raises(ReleaseConstraintsError) as excinfo:
        plan_releases(
            [later, earlier],
            dependencies=[("legacy-2.x", "feature-3.x")],
        )

    assert excinfo.value.blockers == ("legacy-2.x", "feature-3.x")


def test_dependency_is_satisfied_without_bumping_when_already_later():
    earlier = ReleaseLine("legacy-2.x", "2.4.7", priority=5)
    later = ReleaseLine("feature-3.x", "3.0.0-rc.2", priority=10)

    result = plan_releases(
        [earlier, later],
        dependencies=[("feature-3.x", "legacy-2.x")],
    )

    planned = plan_map(result)
    assert planned["legacy-2.x"] == Version.parse("2.4.8-rc.1")
    assert planned["feature-3.x"] == Version.parse("3.0.0-rc.3")
    assert planned["feature-3.x"] > planned["legacy-2.x"]


def test_dependency_skips_consumed_numbers_above_the_bound():
    earlier = ReleaseLine("earlier", "2.0.0-rc.1", priority=10)
    later = ReleaseLine("later", "2.0.0-rc.1", priority=5)
    registry = ReleaseRegistry(
        used=parse(["2.0.0-rc.3"]),
        blocked=parse(["2.0.0-rc.4"]),
    )

    result = plan_releases(
        [earlier, later],
        registry=registry,
        dependencies=[("later", "earlier")],
    )

    planned = plan_map(result)
    assert planned["earlier"] == Version.parse("2.0.0-rc.2")
    assert planned["later"] == Version.parse("2.0.0-rc.5")
    assert planned["later"] > planned["earlier"]


def test_circular_dependencies_are_reported_with_blocking_cycle():
    lines = [
        ReleaseLine("a", "1.0.0"),
        ReleaseLine("b", "1.0.0"),
        ReleaseLine("c", "1.0.0"),
    ]

    with pytest.raises(ReleaseConstraintsError) as excinfo:
        plan_releases(
            lines,
            dependencies=[("a", "b"), ("b", "c"), ("c", "a")],
        )

    blockers = excinfo.value.blockers
    assert blockers[0] == blockers[-1]
    assert set(blockers[:-1]) == {"a", "b", "c"}


def test_self_dependency_is_rejected():
    with pytest.raises(ReleaseConstraintsError) as excinfo:
        plan_releases(
            [ReleaseLine("solo", "1.0.0")],
            dependencies=[("solo", "solo")],
        )

    assert excinfo.value.blockers == ("solo", "solo")


def test_unbridgeable_major_gap_is_reported_with_blocking_lines():
    old_line = ReleaseLine("legacy-1.x", "1.5.0")
    new_line = ReleaseLine("feature-3.x", "3.0.0-rc.2")

    with pytest.raises(ReleaseConstraintsError) as excinfo:
        plan_releases(
            [old_line, new_line],
            dependencies=[("legacy-1.x", "feature-3.x")],
        )

    assert excinfo.value.blockers == ("legacy-1.x", "feature-3.x")
    assert "higher major" in str(excinfo.value)


def test_unknown_dependency_line_is_a_plan_error():
    line = ReleaseLine("known", "1.0.0")

    with pytest.raises(ValueError, match="unknown release line"):
        plan_releases([line], dependencies=[("known", "ghost")])


def test_duplicate_line_names_are_rejected():
    with pytest.raises(ValueError, match="duplicate release line"):
        plan_releases(
            [ReleaseLine("dup", "1.0.0"), ReleaseLine("dup", "2.0.0")]
        )


def test_lines_can_be_given_as_mappings_and_version_objects():
    base = Version.parse("4.1.0")
    result = plan_releases(
        [
            {"name": "dict-line", "base": "4.1.0", "priority": 3},
            ReleaseLine("object-line", base),
        ]
    )

    planned = plan_map(result)
    assert planned["dict-line"] == Version.parse("4.1.1-rc.1")
    assert planned["object-line"] == Version.parse("4.1.1-rc.2")


def test_final_releases_are_planned_without_prerelease_token():
    line = ReleaseLine("stable", "2.3.4", prerelease_token=None)
    registry = ReleaseRegistry(blocked=parse(["2.3.5"]))

    result = plan_releases([line], registry=registry)

    assert plan_map(result)["stable"] == Version.parse("2.3.6")


def test_empty_token_plans_numeric_prereleases():
    line = ReleaseLine("numeric", "1.0.0", prerelease_token="")
    registry = ReleaseRegistry(blocked=parse(["1.0.1-1"]))

    result = plan_releases([line], registry=registry)

    assert plan_map(result)["numeric"] == Version.parse("1.0.1-2")


def test_numeric_prerelease_respects_published_final_sealing():
    line = ReleaseLine("numeric", "1.0.0", prerelease_token="")
    registry = ReleaseRegistry(used=parse(["1.0.1"]))

    result = plan_releases([line], registry=registry)

    planned = plan_map(result)["numeric"]
    assert planned == Version.parse("1.0.2-1")
    assert planned > Version.parse("1.0.1")


def test_invalid_prerelease_token_is_rejected():
    line = ReleaseLine("broken", "1.0.0", prerelease_token="rc.")

    with pytest.raises(ValueError, match="prerelease token"):
        plan_releases([line])


def test_planned_tags_are_unique_within_one_batch():
    lines = [ReleaseLine(f"line-{index}", "1.0.0") for index in range(4)]

    result = plan_releases(lines)

    tags = [str(candidate.planned) for candidate in result]
    assert len(set(tags)) == len(tags)
    assert tags == [
        "1.0.1-rc.1",
        "1.0.1-rc.2",
        "1.0.1-rc.3",
        "1.0.1-rc.4",
    ]


def test_full_story_legacy_and_feature_lines_with_registry():
    legacy = ReleaseLine("legacy-2.x", "2.4.7", priority=10)
    feature = ReleaseLine("feature-3.x", "3.0.0-rc.2", priority=20)
    registry = ReleaseRegistry(
        used=parse(["2.4.8-rc.1", "3.0.0-rc.3"]),
        blocked=parse(["2.4.8-rc.2"]),
    )

    result = plan_releases([legacy, feature], registry=registry)

    assert [(candidate.line, str(candidate.planned)) for candidate in result] == [
        ("feature-3.x", "3.0.0-rc.4"),
        ("legacy-2.x", "2.4.8-rc.3"),
    ]
    for candidate in result:
        assert candidate.planned not in registry.consumed
        assert candidate.planned > candidate.base


def test_full_story_cross_major_dependency_is_unbridgeable():
    legacy = ReleaseLine("legacy-2.x", "2.4.7", priority=10)
    feature = ReleaseLine("feature-3.x", "3.0.0-rc.2", priority=20)

    with pytest.raises(ReleaseConstraintsError) as excinfo:
        plan_releases(
            [legacy, feature],
            dependencies=[("legacy-2.x", "feature-3.x")],
        )

    assert excinfo.value.blockers == ("legacy-2.x", "feature-3.x")
