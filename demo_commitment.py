"""
Runnable demonstration of the two-source publishing commitment analysis.

Scenario:
  source A: every(2).to(4).minutes, must complete 2 collections
  source B: every(3).to(5).minutes, must complete 1 collection
  reconciliation buffer: 2 fixed minutes after both sources are done

Run with:  .venv/bin/python demo_commitment.py
"""

import datetime as dt

from schedule.verification import (
    SourceSpec,
    Verdict,
    analyze_commitment,
    maximum_draws,
    minimum_draws,
    replay_commitment,
)

OPEN = dt.datetime(2026, 11, 14, 9, 0)
SOURCES = [
    SourceSpec("A", min_interval=2, max_interval=4, required_runs=2),
    SourceSpec("B", min_interval=3, max_interval=5, required_runs=1),
]
RECONCILIATION_MINUTES = 2


def describe(horizon_minutes):
    close_at = OPEN + dt.timedelta(minutes=horizon_minutes)
    analysis = analyze_commitment(
        OPEN, close_at, RECONCILIATION_MINUTES, SOURCES
    )
    print(
        "window=%2s min -> %-10s "
        "(earliest ready=%s, latest ready=%s)"
        % (
            horizon_minutes,
            analysis.verdict.value,
            analysis.earliest_publish_ready,
            analysis.latest_publish_ready,
        )
    )
    return close_at


def show_replay(label, close_at, draws):
    report = replay_commitment(
        OPEN, close_at, RECONCILIATION_MINUTES, SOURCES, draws
    )
    print("\n%s trigger sequence: %s" % (label, dict(report.draws_used)))
    for event in report.events:
        print(
            "  %s run %s scheduled %s executed %s (drew %s min)"
            % (
                event.source,
                event.run_index,
                event.scheduled_for.strftime("%H:%M"),
                event.executed_at.strftime("%H:%M"),
                event.drawn_interval,
            )
        )
    print(
        "  completion=%s, publish-ready=%s, deadline=%s -> %s, "
        "margin=%s min, tie order respected=%s"
        % (
            report.completion_at.strftime("%H:%M"),
            report.publish_ready_at.strftime("%H:%M"),
            report.close_at.strftime("%H:%M"),
            "SUCCESS" if report.success else "FAILURE",
            int(report.margin.total_seconds() // 60),
            report.tie_order_respected,
        )
    )


def main():
    print("Exact three-way classification (no random sampling):")
    for horizon in (5, 7, 10):
        describe(horizon)

    print("\n7-minute window is POSSIBLE: witness both outcomes with")
    print("explicit, replayable randint trigger sequences.")
    close_at = OPEN + dt.timedelta(minutes=7)

    show_replay("success", close_at, minimum_draws(SOURCES))
    show_replay("failure", close_at, maximum_draws(SOURCES))

    print(
        "\nReplaying the same sequences always yields the same instants;"
        " verdict of a 7-minute window stays %r." % Verdict.POSSIBLE.value
    )


if __name__ == "__main__":
    main()
