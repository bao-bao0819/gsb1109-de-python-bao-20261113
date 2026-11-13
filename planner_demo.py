"""
End-to-end demonstration of the robust request planner.

Run with:  .venv/bin/python planner_demo.py

The scenario mirrors the operational rules in the task statement:

* the supplier only opens a few short windows;
* a fetch, once started, cannot be paused and must fit inside one window;
* every fetch has duration bounds, a deadline and a value;
* one credential can only run one fetch at a time;
* mandatory ("must-fetch") categories are reserved for the worst-case
  duration and therefore always complete;
* optional fetches fill the slack, including best-effort slots that
  exploit an early finish without endangering any mandatory promise.
"""

from schedule.planner import (
    InfeasibleScheduleError,
    Task,
    Window,
    build_plan,
)


def main() -> None:
    # Three short availability windows (time unit = minutes from horizon start).
    windows = [
        Window(0, 20),
        Window(30, 50),
        Window(60, 80),
    ]

    tasks = [
        # --- must-fetch category: always reserved at duration_max ---
        Task("M1", duration_min=6, duration_max=9, deadline=20,
             value=100, credential="key-a", mandatory=True, category="critical"),
        Task("M2", duration_min=8, duration_max=12, deadline=50,
             value=90, credential="key-a", mandatory=True, category="critical"),
        Task("M3", duration_min=5, duration_max=7, deadline=80,
             value=80, credential="key-b", mandatory=True, category="critical"),
        # --- optional high-value fetches competing for spare capacity ---
        Task("O1", duration_min=4, duration_max=6, deadline=50,
             value=60, credential="key-a", category="market"),
        Task("O2", duration_min=3, duration_max=8, deadline=80,
             value=55, credential="key-b", category="market"),
        Task("O3", duration_min=2, duration_max=6, deadline=20,
             value=20, credential="key-a", category="market"),
    ]

    print("=" * 70)
    print("SCENARIO 1: mandatory tasks can be guaranteed")
    print("=" * 70)
    plan = build_plan(tasks, windows)
    plan.validate()
    print(plan.describe())

    print()
    print("Spare capacity left per credential:")
    for credential, fragments in plan.free_intervals().items():
        formatted = [
            "W%d:[%s, %s)" % (index, start, end)
            for index, start, end in fragments
        ]
        print("  %s -> %s" % (credential, ", ".join(formatted) or "none"))

    print()
    print("Why this is guaranteed: every mandatory reservation reserves")
    print("duration_max, so any realized duration within the declared bounds")
    print("still finishes before its deadline and inside its window.")
    print("Best-effort slots only occupy that spare capacity and are clearly")
    print("flagged, so an unlucky long fetch can never displace a mandatory one.")

    print()
    print("=" * 70)
    print("SCENARIO 2: mandatory load exceeds what the windows can guarantee")
    print("=" * 70)
    overloaded = [
        Task("X1", duration_min=15, duration_max=18, deadline=20,
             value=100, credential="key-a", mandatory=True),
        Task("X2", duration_min=15, duration_max=18, deadline=20,
             value=100, credential="key-a", mandatory=True),
    ]
    try:
        build_plan(overloaded, windows)
    except InfeasibleScheduleError as error:
        print("Correctly refused to promise the impossible:")
        print(" ", error)


if __name__ == "__main__":
    main()
