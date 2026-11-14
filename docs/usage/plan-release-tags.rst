.. _plan-release-tags:

Planning Release Tags Across Release Lines
==========================================

When an old maintenance line and a current feature line register release
candidates into the same repository, each line needs the next free tag
derived from its own base version. The :mod:`semver.release` module plans
these tags in one batch.

A :class:`~semver.release.ReleaseLine` carries a base version and a
priority. Lines with a higher priority are planned first and claim the
lowest free tag:

.. code-block:: python

    >>> lines = [
    ...     semver.ReleaseLine("legacy-2.x", "2.4.7", priority=10),
    ...     semver.ReleaseLine("feature-2.x", "2.5.0-rc.2", priority=20),
    ... ]
    >>> for candidate in semver.plan_releases(lines):
    ...     print(candidate)
    feature-2.x: 2.5.0-rc.3
    legacy-2.x: 2.4.8-rc.1


Reserved, Revoked and Published Numbers
---------------------------------------

A :class:`~semver.release.ReleaseRegistry` collects the numbers the
planner must skip. Numbers already sent to customers go into ``used`` and
can never be reassigned, even when the release was withdrawn. Numbers that
were only reserved or revoked before publication go into ``blocked``:

.. code-block:: python

    >>> registry = semver.ReleaseRegistry(
    ...     used=["2.4.8-rc.1"],
    ...     blocked=["2.4.8-rc.2"],
    ... )
    >>> plan = semver.plan_releases(lines[:1], registry=registry)
    >>> str(plan[0].planned)
    '2.4.8-rc.3'

Build metadata never participates in precedence and is never part of a
planned tag, so a ``+build`` suffix cannot be used to claim a higher
priority:

.. code-block:: python

    >>> registry = semver.ReleaseRegistry(used=["1.0.1-rc.1+build.9"])
    >>> line = semver.ReleaseLine("main", "1.0.1-rc.0")
    >>> str(semver.plan_releases([line], registry=registry)[0].planned)
    '1.0.1-rc.2'


Ordering Constraints Between Candidates
---------------------------------------

A consumer can declare that one candidate must be released after another.
Pass the constraint as a ``(later, earlier)`` pair:

.. code-block:: python

    >>> plan = semver.plan_releases(
    ...     lines,
    ...     dependencies=[("legacy-2.x", "feature-2.x")],
    ... )
    >>> [(candidate.line, str(candidate.planned)) for candidate in plan]
    [('feature-2.x', '2.5.0-rc.3'), ('legacy-2.x', '2.5.0-rc.4')]

When the constraints lock each other, a
:class:`~semver.release.ReleaseConstraintsError` names the blocking lines:

.. code-block:: python

    >>> try:
    ...     semver.plan_releases(
    ...         [
    ...             semver.ReleaseLine("a", "1.0.0"),
    ...             semver.ReleaseLine("b", "1.0.0"),
    ...         ],
    ...         dependencies=[("a", "b"), ("b", "a")],
    ...     )
    ... except semver.ReleaseConstraintsError as error:
    ...     print(error.blockers)
    ('a', 'b', 'a')
