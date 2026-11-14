from itertools import permutations

import pytest

from referencing import Registry, exceptions
from referencing._core import _settle
from referencing.policy import Policy, contains
from referencing.tests.test_core import ID_AND_CHILDREN

HQ = "https://corp.example/schemas/"
REGION = "https://corp.example/schemas/regions/east/"
PROJECT = "https://corp.example/schemas/regions/east/projects/alpha/"
OTHER = "https://other.example/schemas/"


def resource(contents):
    return ID_AND_CHILDREN.create_resource(contents)


def registry(scopes, *pairs):
    return (
        Registry()
        .with_policy(list(scopes))
        .with_resources(
            [(uri, resource(contents)) for uri, contents in pairs],
        )
    )


class TestPolicy:
    def test_scopes_are_stored(self):
        policy = Policy.from_scopes([HQ, REGION])
        assert set(policy.scopes) == {HQ, REGION}

    def test_empty_policy_covers_nothing(self):
        assert not Policy().covers(HQ)
        assert not Policy().scopes

    def test_policy_accepts_any_iterable(self):
        assert set(Policy.from_scopes({HQ}).scopes) == {HQ}
        assert set(Policy(scopes=[HQ]).scopes) == {HQ}

    @pytest.mark.parametrize(
        ("prefix", "uri", "expected"),
        [
            (HQ, HQ, True),
            (HQ, HQ + "a/b", True),
            ("https://example.com/a", "https://example.com/a/b", True),
            ("https://example.com/a", "https://example.com/ab", False),
            ("https://example.com/a/", "https://example.com/a", False),
            ("urn:example:a", "urn:example:a:b", True),
            ("urn:example:a", "urn:example:ab", False),
            ("urn:example:a:", "urn:example:a:b", True),
            ("urn:example:a", "https://example.com/a", False),
        ],
    )
    def test_contains(self, prefix, uri, expected):
        assert contains(prefix, uri) is expected

    def test_covers(self):
        policy = Policy.from_scopes([REGION])
        assert policy.covers(REGION + "doc")
        assert not policy.covers(HQ + "doc")
        assert not policy.covers(OTHER + "doc")

    def test_narrowest_of_multiple_scopes(self):
        policy = Policy.from_scopes([HQ, REGION])
        assert policy.covers(REGION + "doc")
        assert policy._narrowest(policy._claims(REGION + "doc")) == REGION

    def test_relationship_only_one_side_claims(self):
        hq = Policy.from_scopes([HQ])
        other = Policy.from_scopes([OTHER])
        assert hq.relationship(HQ + "x", other) == "only-self"
        assert other.relationship(HQ + "x", hq) == "only-other"

    def test_relationship_narrower_and_equal(self):
        hq = Policy.from_scopes([HQ])
        region = Policy.from_scopes([REGION])
        assert region.relationship(REGION + "x", hq) == "self-narrower"
        assert hq.relationship(REGION + "x", region) == "other-narrower"

        again = Policy.from_scopes([HQ])
        assert hq.relationship(HQ + "x", again) == "equal"

    def test_relationship_unrelated(self):
        first = Policy.from_scopes(["https://a.example/"])
        second = Policy.from_scopes(["https://b.example/"])
        assert first.relationship("urn:shared:x", second) == "unrelated"


class TestSettle:
    def test_single_candidate_wins(self):
        only = object()
        assert _settle(
            "urn:x",
            [(only, Policy())],
            governed=False,
            alias=False,
        ) == (only, False)

    def test_neutral_policies_keep_last_writer(self):
        first = object()
        second = object()
        winner, blocked = _settle(
            "urn:x",
            [(first, Policy()), (second, Policy())],
            governed=False,
            alias=False,
        )
        assert winner is second
        assert not blocked

    def test_duplicate_candidates_are_ignored(self):
        only = object()
        policy = Policy()
        winner, blocked = _settle(
            "urn:x",
            [(only, policy), (only, policy)],
            governed=False,
            alias=False,
        )
        assert winner is only
        assert not blocked

    def test_unrelated_covering_ranges_block(self):
        first = object()
        second = object()
        winner, blocked = _settle(
            "https://a.example/x",
            [
                (first, Policy.from_scopes(["https://a.example/"])),
                (second, Policy.from_scopes(["https://a.example/x/"])),
            ],
            governed=False,
            alias=False,
        )
        assert winner is first
        assert not blocked

        winner, blocked = _settle(
            "urn:shared:x",
            [
                (first, Policy.from_scopes(["https://a.example/"])),
                (second, Policy.from_scopes(["https://b.example/"])),
            ],
            governed=False,
            alias=False,
        )
        assert winner is None
        assert blocked

    def test_neutral_definition_survives_ungoverned_declarers(self):
        neutral = object()
        declarer = object()
        winner, blocked = _settle(
            "urn:shared:x",
            [
                (neutral, Policy()),
                (declarer, Policy.from_scopes(["https://a.example/"])),
            ],
            governed=False,
            alias=False,
        )
        assert winner is neutral
        assert not blocked

    def test_alias_filters_unauthorized_definition(self):
        authorized = object()
        rogue = object()
        winner, blocked = _settle(
            HQ + "doc",
            [
                (authorized, Policy.from_scopes([HQ])),
                (rogue, Policy.from_scopes([OTHER])),
            ],
            governed=True,
            alias=True,
        )
        assert winner is authorized
        assert not blocked

    def test_alias_keeps_narrower_definition_among_authorized(self):
        broad = object()
        narrow = object()
        winner, blocked = _settle(
            REGION + "doc",
            [
                (broad, Policy.from_scopes([HQ])),
                (narrow, Policy.from_scopes([REGION])),
            ],
            governed=True,
            alias=True,
        )
        assert winner is narrow
        assert not blocked

    def test_alias_with_no_admissible_definition_blocks(self):
        winner, blocked = _settle(
            HQ + "doc",
            [
                (object(), Policy.from_scopes([OTHER])),
                (object(), Policy.from_scopes(["https://third.example/"])),
            ],
            governed=True,
            alias=True,
        )
        assert winner is None
        assert blocked

    def test_alias_admits_neutral_policy(self):
        neutral = object()
        rogue = object()
        winner, blocked = _settle(
            HQ + "doc",
            [
                (neutral, Policy()),
                (rogue, Policy.from_scopes([OTHER])),
            ],
            governed=True,
            alias=True,
        )
        assert winner is neutral
        assert not blocked

    def test_identical_value_under_two_policies_wins(self):
        shared = object()
        winner, blocked = _settle(
            HQ + "doc",
            [
                (shared, Policy.from_scopes([HQ])),
                (shared, Policy.from_scopes([REGION])),
            ],
            governed=False,
            alias=False,
        )
        assert winner is shared
        assert not blocked


class TestRegistryAuthorization:
    def test_nested_ranges_override_regardless_of_order(self):
        hq = registry(
            [HQ],
            (REGION + "r1", {"v": "hq"}),
            (PROJECT + "p1", {"v": "hq"}),
        )
        region = registry(
            [REGION],
            (REGION + "r1", {"v": "region"}),
            (PROJECT + "p1", {"v": "region"}),
        )
        project = registry([PROJECT], (PROJECT + "p1", {"v": "project"}))

        for order in permutations([hq, region, project]):
            combined = order[0].combine(*order[1:])
            assert combined.contents(REGION + "r1") == {"v": "region"}
            assert combined.contents(PROJECT + "p1") == {"v": "project"}
            assert combined.blocking_resource_conflicts() == {}

    def test_out_of_range_contributor_is_inadmissible(self):
        authorized = registry(
            ["https://a.example/x/"],
            ("https://a.example/x/t", {"v": "A"}),
        )
        rogue = registry(
            ["https://b.example/x/"],
            ("https://a.example/x/t", {"v": "B"}),
        )
        for order in permutations([authorized, rogue]):
            combined = order[0].combine(order[1])
            assert combined.blocking_resource_conflicts() == {}
            assert combined.contents("https://a.example/x/t") == {"v": "A"}

    def test_ungoverned_collision_blocks_retrieval(self):
        first = registry(["https://a.example/"], ("urn:shared:x", {"v": "A"}))
        second = registry(["https://b.example/"], ("urn:shared:x", {"v": "B"}))
        combined = first.combine(second)

        conflicts = combined.blocking_resource_conflicts()
        assert set(conflicts) == {"urn:shared:x"}
        assert {
            frozenset(each.contents.items())
            for each in conflicts["urn:shared:x"]
        } == {frozenset({("v", "A")}), frozenset({("v", "B")})}
        with pytest.raises(exceptions.PendingBlockingConflict):
            combined["urn:shared:x"]
        with pytest.raises(exceptions.PendingBlockingConflict):
            combined.contents("urn:shared:x")
        with pytest.raises(exceptions.PendingBlockingConflict):
            combined.get_or_retrieve("urn:shared:x")

    def test_equal_range_identical_definitions_merge(self):
        contents = {"v": 1}
        combined = registry([HQ], (HQ + "x", contents)).combine(
            registry([HQ], (HQ + "x", dict(contents))),
        )
        assert combined.contents(HQ + "x") == contents
        assert combined.blocking_resource_conflicts() == {}

    def test_equal_range_differing_definitions_conflict(self):
        combined = registry([HQ], (HQ + "x", {"v": 1})).combine(
            registry([HQ], (HQ + "x", {"v": 2})),
        )
        assert HQ + "x" in combined.blocking_resource_conflicts()

    def test_blocked_uri_surfaces_through_resolver(self):
        first = registry(["https://a.example/"], ("urn:shared:x", {"v": "A"}))
        second = registry(["https://b.example/"], ("urn:shared:x", {"v": "B"}))
        combined = first.combine(second)
        with pytest.raises(exceptions.Unresolvable):
            combined.resolver().lookup("urn:shared:x")

    def test_uncontested_entries_remain_available(self):
        first = registry(
            ["https://a.example/"],
            ("urn:shared:x", {"v": "A"}),
            ("https://a.example/own", {"v": "a"}),
        )
        second = registry(
            ["https://b.example/"],
            ("urn:shared:x", {"v": "B"}),
            ("https://b.example/own", {"v": "b"}),
        )
        combined = first.combine(second)
        assert combined.contents("https://a.example/own") == {"v": "a"}
        assert combined.contents("https://b.example/own") == {"v": "b"}

    def test_covering_authority_resolves_existing_conflict(self):
        first = registry(["https://a.example/"], ("urn:shared:x", {"v": "A"}))
        second = registry(["https://b.example/"], ("urn:shared:x", {"v": "B"}))
        blocked = first.combine(second)
        assert blocked.blocking_resource_conflicts()

        covering = registry(["urn:shared:"], ("urn:shared:x", {"v": "C"}))
        resolved = blocked.combine(covering)
        assert resolved.contents("urn:shared:x") == {"v": "C"}
        assert resolved.blocking_resource_conflicts() == {}

    def test_with_resource_resolves_existing_conflict(self):
        first = registry(["https://a.example/"], ("urn:shared:x", {"v": "A"}))
        second = registry(["https://b.example/"], ("urn:shared:x", {"v": "B"}))
        blocked = first.combine(second)

        covering = blocked.with_policy(["urn:shared:"]).with_resource(
            "urn:shared:x",
            resource({"v": "C"}),
        )
        assert covering.contents("urn:shared:x") == {"v": "C"}
        assert covering.blocking_resource_conflicts() == {}

    def test_with_resource_keeps_unresolved_conflict(self):
        first = registry(["https://a.example/"], ("urn:shared:x", {"v": "A"}))
        second = registry(["https://b.example/"], ("urn:shared:x", {"v": "B"}))
        blocked = first.combine(second)

        still_blocked = blocked.with_policy(
            ["https://d.example/"],
        ).with_resource("urn:shared:x", resource({"v": "D"}))
        assert set(still_blocked.blocking_resource_conflicts()) == {
            "urn:shared:x",
        }
        with pytest.raises(exceptions.PendingBlockingConflict):
            still_blocked.contents("urn:shared:x")

    def test_equal_scope_conflict_via_with_resources(self):
        scoped = registry([HQ], (HQ + "x", {"v": 1}))
        contested = scoped.with_policy([HQ]).with_resource(
            HQ + "x",
            resource({"v": 2}),
        )
        assert HQ + "x" in contested.blocking_resource_conflicts()

    def test_with_resources_conflict_seals_existing_aliases(self):
        crawled = (
            Registry()
            .with_policy([REGION])
            .with_resource(
                REGION + "doc",
                resource({"anchors": {"k": {"who": "one"}}}),
            )
            .crawl()
        )
        contested = crawled.with_policy([REGION]).with_resource(
            REGION + "doc",
            resource({"anchors": {"k": {"who": "two"}}}),
        )
        assert REGION + "doc" in contested.blocking_resource_conflicts()
        assert (REGION + "doc", "k") in (contested.blocking_anchor_conflicts())
        with pytest.raises(exceptions.PendingBlockingConflict):
            contested.anchor(REGION + "doc", "k")

    def test_conflict_survives_combine_on_either_side(self):
        first = registry(["https://a.example/"], ("urn:shared:x", {"v": "A"}))
        second = registry(["https://b.example/"], ("urn:shared:x", {"v": "B"}))
        blocked = first.combine(second)

        assert Registry().combine(blocked).blocking_resource_conflicts() == (
            blocked.blocking_resource_conflicts()
        )

    def test_known_conflict_is_not_duplicated_when_recombined(self):
        left = (
            Registry()
            .with_policy(["https://a.example/"])
            .with_resource("urn:shared:x", resource({"v": "A"}))
        )
        right = (
            Registry()
            .with_policy(["https://b.example/"])
            .with_resource("urn:shared:x", resource({"v": "B"}))
        )
        blocked = left.combine(right)
        recombined = blocked.combine(blocked.__class__().combine(blocked))
        conflicts = recombined.blocking_resource_conflicts()
        assert set(conflicts) == {"urn:shared:x"}
        assert len(conflicts["urn:shared:x"]) == 2

    def test_narrower_authority_resolves_anchor_conflict(self):
        team = REGION + "team/"
        one = (
            Registry()
            .with_policy([REGION])
            .with_resource(
                team + "doc",
                resource({"anchors": {"k": {"who": "one"}}}),
            )
            .crawl()
        )
        two = (
            Registry()
            .with_policy([REGION])
            .with_resource(
                team + "doc",
                resource({"anchors": {"k": {"who": "two"}}}),
            )
            .crawl()
        )
        blocked = one.combine(two)
        assert (team + "doc", "k") in blocked.blocking_anchor_conflicts()

        decisive = (
            Registry()
            .with_policy([team])
            .with_resource(
                team + "doc",
                resource({"anchors": {"k": {"who": "decisive"}}}),
            )
            .crawl()
        )
        resolved = blocked.combine(decisive)
        assert resolved.blocking_anchor_conflicts() == {}
        assert (
            resolved.anchor(team + "doc", "k").value.resource.contents["who"]
            == "decisive"
        )

    def test_with_policy_accepts_policy_object(self):
        policy = Policy.from_scopes([HQ])
        scoped = Registry().with_policy(policy)
        assert scoped._policy is policy

    def test_combine_adopts_declared_policy(self):
        scoped = registry([HQ], (HQ + "x", {"v": 1}))
        combined = Registry().combine(scoped)
        assert set(combined._policy.scopes) == {HQ}

    def test_repr_reports_conflicts(self):
        first = registry(
            ["https://a.example/"],
            ("urn:shared:x", {"v": "A"}),
            ("urn:shared:z", {"v": "A2"}),
        )
        second = registry(["https://b.example/"], ("urn:shared:x", {"v": "B"}))
        assert "1 blocking conflict" in repr(first.combine(second))

        third = registry(["https://c.example/"], ("urn:shared:z", {"v": "C"}))
        assert "2 blocking conflicts" in repr(first.combine(second, third))


class TestRegistryAnchors:
    def _anchor_registry(self, scopes, uri, who):
        return (
            Registry()
            .with_policy(list(scopes))
            .with_resource(
                uri,
                resource({"anchors": {"shared": {"who": who}}}),
            )
            .crawl()
        )

    def test_unauthorized_alias_cannot_override(self):
        hq = self._anchor_registry([HQ], HQ + "doc", "hq")
        rogue = self._anchor_registry([OTHER], HQ + "doc", "rogue")
        for order in permutations([hq, rogue]):
            combined = order[0].combine(order[1])
            assert (
                combined.anchor(HQ + "doc", "shared").value.resource.contents[
                    "who"
                ]
                == "hq"
            )
            assert combined.blocking_anchor_conflicts() == {}

    def test_equal_scope_anchor_conflict(self):
        first = self._anchor_registry([REGION], REGION + "doc", "one")
        second = self._anchor_registry([REGION], REGION + "doc", "two")
        combined = first.combine(second)
        assert set(combined.blocking_anchor_conflicts()) == {
            (REGION + "doc", "shared"),
        }
        with pytest.raises(exceptions.PendingBlockingConflict):
            combined.anchor(REGION + "doc", "shared")
        with pytest.raises(exceptions.Unresolvable):
            combined.resolver().lookup(REGION + "doc#shared")

    def test_blocking_resource_seals_its_aliases(self):
        first = (
            Registry()
            .with_policy(["https://a.example/"])
            .with_resource(
                "urn:shared:x",
                resource({"anchors": {"k": {"who": "a"}}}),
            )
            .crawl()
        )
        second = (
            Registry()
            .with_policy(["https://b.example/"])
            .with_resource(
                "urn:shared:x",
                resource({"anchors": {"k": {"who": "b"}}}),
            )
            .crawl()
        )
        combined = first.combine(second)
        assert ("urn:shared:x", "k") in combined.blocking_anchor_conflicts()
        with pytest.raises(exceptions.PendingBlockingConflict):
            combined.anchor("urn:shared:x", "k")

    def test_blocking_resource_seals_alias_only_incoming_registry_has(self):
        first = (
            Registry()
            .with_policy(["https://a.example/"])
            .with_resource(
                "urn:shared:x",
                resource({"ID": "urn:shared:x"}),
            )
        )
        second = (
            Registry()
            .with_policy(["https://b.example/"])
            .with_resource(
                "urn:shared:x",
                resource(
                    {
                        "ID": "urn:shared:x",
                        "anchors": {"k": {"who": "b"}},
                    },
                ),
            )
            .crawl()
        )
        combined = first.combine(second)
        assert ("urn:shared:x", "k") in combined.blocking_anchor_conflicts()

    def test_blocking_resource_seals_alias_only_existing_registry_has(self):
        first = (
            Registry()
            .with_policy(["https://a.example/"])
            .with_resource(
                "urn:shared:x",
                resource(
                    {
                        "ID": "urn:shared:x",
                        "anchors": {"k": {"who": "a"}},
                    },
                ),
            )
            .crawl()
        )
        second = (
            Registry()
            .with_policy(["https://b.example/"])
            .with_resource(
                "urn:shared:x",
                resource({"ID": "urn:shared:x"}),
            )
        )
        combined = first.combine(second)
        assert ("urn:shared:x", "k") in combined.blocking_anchor_conflicts()

    def test_anchor_blocked_after_crawl(self):
        scoped = Registry().with_policy([REGION])
        uncrawled = scoped.with_resource(
            REGION + "root",
            resource(
                {
                    "ID": REGION + "root",
                    "children": [
                        {
                            "ID": REGION + "child",
                            "anchors": {"k": {"who": "first"}},
                        },
                        {
                            "ID": REGION + "child",
                            "anchors": {"k": {"who": "second"}},
                        },
                    ],
                },
            ),
        )
        crawled = uncrawled.crawl()
        assert REGION + "child" in crawled.blocking_resource_conflicts()
        with pytest.raises(exceptions.PendingBlockingConflict):
            uncrawled.anchor(REGION + "child", "k")

    def test_duplicate_subresource_anchor_conflicts_during_crawl(self):
        first = (
            Registry()
            .with_policy(["urn:shared"])
            .with_resource(
                "urn:r1",
                resource(
                    {
                        "ID": "urn:r1",
                        "children": [
                            {
                                "ID": "urn:shared",
                                "anchors": {"k": {"who": "one"}},
                            },
                        ],
                    },
                ),
            )
        )
        second = (
            Registry()
            .with_policy(["urn:shared"])
            .with_resource(
                "urn:r2",
                resource(
                    {
                        "ID": "urn:r2",
                        "children": [
                            {
                                "ID": "urn:shared",
                                "anchors": {"k": {"who": "two"}},
                            },
                        ],
                    },
                ),
            )
        )
        crawled = first.combine(second).crawl()
        assert ("urn:shared", "k") in crawled.blocking_anchor_conflicts()
        with pytest.raises(exceptions.PendingBlockingConflict):
            crawled.anchor("urn:shared", "k")

    def test_duplicate_subresource_equal_anchor_merges_during_crawl(self):
        first = Registry().with_resource(
            "urn:r1",
            resource(
                {
                    "ID": "urn:r1",
                    "children": [
                        {"ID": "urn:shared", "anchors": {"k": {"who": "one"}}},
                    ],
                },
            ),
        )
        second = Registry().with_resource(
            "urn:r2",
            resource(
                {
                    "ID": "urn:r2",
                    "children": [
                        {"ID": "urn:shared", "anchors": {"k": {"who": "one"}}},
                    ],
                },
            ),
        )
        crawled = first.combine(second).crawl()
        assert crawled.blocking_anchor_conflicts() == {}
        assert crawled.anchor("urn:shared", "k").value.resource.contents == {
            "who": "one",
        }

    def test_crawl_resettles_anchor_conflict_left_by_combine(self):
        team = REGION + "team/"
        one = (
            Registry()
            .with_policy([REGION])
            .with_resource(
                team + "doc",
                resource({"anchors": {"k": {"who": "one"}}}),
            )
            .crawl()
        )
        two = (
            Registry()
            .with_policy([REGION])
            .with_resource(
                team + "doc",
                resource({"anchors": {"k": {"who": "two"}}}),
            )
            .crawl()
        )
        blocked = one.combine(two)
        assert (team + "doc", "k") in blocked.blocking_anchor_conflicts()

        decisive = (
            Registry()
            .with_policy([team])
            .with_resource(
                team + "root",
                resource(
                    {
                        "ID": team + "root",
                        "children": [
                            {
                                "ID": team + "doc",
                                "anchors": {"k": {"who": "decisive"}},
                            },
                        ],
                    },
                ),
            )
        )
        resolved = blocked.combine(decisive).crawl()
        assert resolved.blocking_resource_conflicts() == {}
        assert (
            resolved.anchor(team + "doc", "k").value.resource.contents["who"]
            == "decisive"
        )
        assert resolved.blocking_anchor_conflicts() == {}


class TestCrawlAuthorization:
    def test_discovered_subresources_keep_contributing_policy(self):
        roots = [
            (
                HQ,
                HQ + "root",
                {
                    "ID": HQ + "root",
                    "children": [
                        {"ID": REGION + "sub", "v": "hq"},
                        {"ID": PROJECT + "deep", "v": "hq"},
                    ],
                },
            ),
            (
                REGION,
                REGION + "root",
                {
                    "ID": REGION + "root",
                    "children": [
                        {"ID": REGION + "sub", "v": "region"},
                        {"ID": PROJECT + "deep", "v": "region"},
                    ],
                },
            ),
            (
                PROJECT,
                PROJECT + "root",
                {
                    "ID": PROJECT + "root",
                    "children": [{"ID": PROJECT + "deep", "v": "project"}],
                },
            ),
        ]
        registries = [
            Registry()
            .with_policy([scope])
            .with_resource(uri, resource(contents))
            for scope, uri, contents in roots
        ]
        for order in permutations(registries):
            crawled = order[0].combine(*order[1:]).crawl()
            assert crawled.contents(REGION + "sub") == {
                "ID": REGION + "sub",
                "v": "region",
            }
            assert crawled.contents(PROJECT + "deep") == {
                "ID": PROJECT + "deep",
                "v": "project",
            }
            assert crawled.blocking_resource_conflicts() == {}

    def test_discovered_ungoverned_subresources_conflict(self):
        first = (
            Registry()
            .with_policy(["urn:a/"])
            .with_resource(
                "urn:a/root",
                resource(
                    {
                        "ID": "urn:a/root",
                        "children": [{"ID": "urn:shared:x", "v": "A"}],
                    },
                ),
            )
        )
        second = (
            Registry()
            .with_policy(["urn:b/"])
            .with_resource(
                "urn:b/root",
                resource(
                    {
                        "ID": "urn:b/root",
                        "children": [{"ID": "urn:shared:x", "v": "B"}],
                    },
                ),
            )
        )
        crawled = first.combine(second).crawl()
        assert set(crawled.blocking_resource_conflicts()) == {"urn:shared:x"}
        with pytest.raises(exceptions.PendingBlockingConflict):
            crawled.get_or_retrieve("urn:shared:x")

    def test_get_or_retrieve_conflict_only_revealed_by_crawl(self):
        uncrawled = (
            Registry()
            .with_policy(["urn:a/"])
            .with_resource(
                "urn:a/root",
                resource(
                    {
                        "ID": "urn:a/root",
                        "children": [{"ID": "urn:shared:x", "v": "A"}],
                    },
                ),
            )
        )
        other = (
            Registry()
            .with_policy(["urn:b/"])
            .with_resource(
                "urn:b/root",
                resource(
                    {
                        "ID": "urn:b/root",
                        "children": [{"ID": "urn:shared:x", "v": "B"}],
                    },
                ),
            )
        )
        combined = uncrawled.combine(other)
        with pytest.raises(exceptions.PendingBlockingConflict):
            combined.get_or_retrieve("urn:shared:x")

    def test_covering_subresource_resolves_conflict_during_crawl(self):
        first = (
            Registry()
            .with_policy(["urn:a/"])
            .with_resource(
                "urn:a/root",
                resource(
                    {
                        "ID": "urn:a/root",
                        "children": [{"ID": "urn:shared:x", "v": "A"}],
                    },
                ),
            )
        )
        second = (
            Registry()
            .with_policy(["urn:b/"])
            .with_resource(
                "urn:b/root",
                resource(
                    {
                        "ID": "urn:b/root",
                        "children": [{"ID": "urn:shared:x", "v": "B"}],
                    },
                ),
            )
        )
        blocked = first.combine(second).crawl()
        assert blocked.blocking_resource_conflicts()

        covering = (
            Registry()
            .with_policy(["urn:shared:"])
            .with_resource(
                "urn:parent/root",
                resource(
                    {
                        "ID": "urn:parent/root",
                        "children": [{"ID": "urn:shared:x", "v": "C"}],
                    },
                ),
            )
        )
        resolved = blocked.combine(covering).crawl()
        assert resolved.contents("urn:shared:x") == {
            "ID": "urn:shared:x",
            "v": "C",
        }
        assert resolved.blocking_resource_conflicts() == {}


class TestConflictMaintenance:
    def _blocked(self):
        first = (
            Registry()
            .with_policy(["https://a.example/"])
            .with_resource(
                "urn:shared:x",
                resource({"anchors": {"k": {"who": "a"}}}),
            )
            .crawl()
        )
        second = (
            Registry()
            .with_policy(["https://b.example/"])
            .with_resource(
                "urn:shared:x",
                resource({"anchors": {"k": {"who": "b"}}}),
            )
            .crawl()
        )
        return first.combine(second)

    def test_blocked_uri_is_not_present_for_removal(self):
        blocked = self._blocked()
        with pytest.raises(exceptions.NoSuchResource):
            blocked.remove("urn:shared:x")

    def test_remove_unrelated_uri_keeps_conflict(self):
        blocked = self._blocked()
        with_other = blocked.with_resource("urn:other", resource({}))
        cleaned = with_other.remove("urn:other")
        assert set(blocked.blocking_resource_conflicts()) == {"urn:shared:x"}
        assert set(cleaned.blocking_resource_conflicts()) == {"urn:shared:x"}

    def test_remove_keeps_unrelated_conflicts_and_drops_own_anchors(self):
        blocked = (
            self._blocked()
            .with_resource(
                REGION + "doc",
                resource({"anchors": {"k": {"who": "kept"}}}),
            )
            .crawl()
        )
        assert (REGION + "doc", "k") in blocked._anchors

        cleaned = blocked.remove(REGION + "doc")
        assert set(cleaned.blocking_resource_conflicts()) == {"urn:shared:x"}
        assert ("urn:shared:x", "k") in cleaned.blocking_anchor_conflicts()
        assert REGION + "doc" not in cleaned
        assert (REGION + "doc", "k") not in cleaned._anchors
        assert (REGION + "doc", "k") not in cleaned._anchor_policies

    def test_canonical_anchor_conflict_is_enforced(self):
        canonical = "urn:canonical"
        first = (
            Registry()
            .with_policy([canonical])
            .with_resource(
                "urn:external-1",
                resource({"ID": canonical, "anchors": {"k": {"who": "one"}}}),
            )
            .crawl()
        )
        second = (
            Registry()
            .with_policy([canonical])
            .with_resource(
                "urn:external-2",
                resource({"ID": canonical, "anchors": {"k": {"who": "two"}}}),
            )
            .crawl()
        )
        combined = first.combine(second)
        with pytest.raises(exceptions.PendingBlockingConflict):
            combined.anchor("urn:external-1", "k")

    def test_empty_fragment_uri_conflict(self):
        first = registry(["https://a.example/"], ("urn:shared:x", {"v": "A"}))
        second = registry(["https://b.example/"], ("urn:shared:x", {"v": "B"}))
        combined = first.combine(second)
        with pytest.raises(exceptions.PendingBlockingConflict):
            combined["urn:shared:x#"]

    def test_pending_conflict_message(self):
        error = exceptions.PendingBlockingConflict(ref="urn:shared:x")
        assert "urn:shared:x" in str(error)
        assert isinstance(error, exceptions.Unresolvable)

    def test_blocking_anchor_conflicts_excludes_resources(self):
        resource_conflict = registry(
            ["https://a.example/"],
            ("urn:shared:x", {"v": "A"}),
        ).combine(
            registry(["https://b.example/"], ("urn:shared:x", {"v": "B"})),
        )
        assert resource_conflict.blocking_anchor_conflicts() == {}
        assert (
            "urn:shared:x" in resource_conflict.blocking_resource_conflicts()
        )
