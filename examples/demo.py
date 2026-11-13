"""
End-to-end demonstration of workstation reconstruction.

Run with::

    python -m examples.demo
"""
from __future__ import annotations

import os

from shift_scheduling import JobLog, Reconstruction, verify
from shift_scheduling.logio import load_jobs, load_station_roster


HERE = os.path.dirname(os.path.abspath(__file__))


STATIONS = {
    "A": ("A1", "A2"),
    "B": ("B1",),
}


def banner(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def print_schedule(name, jobs, stations_by_type, schedule):
    print("\n%s" % name)
    print("-" * 40)
    violations = verify(jobs, stations_by_type, schedule)
    jobs_by_id = {job.job_id: job for job in jobs}
    all_stations = [
        station
        for stations in stations_by_type.values()
        for station in stations
    ]
    for station in sorted(all_stations):
        placed = sorted(
            (
                (jobs_by_id[job_id].start, jobs_by_id[job_id].end, job_id)
                for job_id, assigned in schedule.items()
                if assigned == station
            )
        )
        chain = ", ".join(
            "%s[%d,%d)" % (job_id, start, end) for start, end, job_id in placed
        )
        print("  %s: %s" % (station, chain or "(idle all shift)"))
    print("  独立校验: %s" % ("全部约束满足" if not violations else violations))


def main():
    banner("场景一：可排产班次 —— 唯一归属 / 歧义归属 / 空闲窗口")
    jobs = [
        JobLog("J1", 0, 12, "A", pinned_station="A1"),  # 巡检锁定 A1
        JobLog("J2", 0, 12, "A"),                        # 与 J1 重叠，被迫去 A2
        JobLog("J3", 12, 24, "A"),                       # 两台 A 工位全空，歧义
        JobLog("J4", 24, 36, "A"),                       # 端点衔接 J3，同样有歧义
        JobLog("J5", 0, 40, "B"),                        # B 型仅 B1，唯一
    ]
    engine = Reconstruction(jobs, STATIONS)
    report = engine.classify()

    print("枚举到的整班排位总数: %d" % len(report["solutions"]))
    print("唯一归属的单: %s" % report["unique"])
    print("存在不同归属的单: %s" % report["ambiguous"])
    print("排不下的单: %s" % report["unschedulable_jobs"])
    print("各工位空闲窗口:")
    for station, gaps in report["idle_windows"].items():
        rendered = ", ".join("[%d,%d)" % (g["begin"], g["end"]) for g in gaps)
        print("  %-16s %s" % (station, rendered or "无"))

    schedule_one, schedule_two = engine.demonstrate_ambiguity("J3")
    print_schedule("排位甲（J3 的一种归属）", jobs, STATIONS, schedule_one)
    print_schedule("排位乙（J3 的另一种归属）", jobs, STATIONS, schedule_two)
    print(
        "\n两份排位中 J3 分别落在 %s / %s，"
        "其余日志（含巡检锁定）全部满足。"
        % (schedule_one["J3"], schedule_two["J3"])
    )
    print("排位甲设备利用率: %s" % engine.utilization(schedule_one))

    csv_jobs = load_jobs(os.path.join(HERE, "sample_shift.csv"))
    csv_roster = load_station_roster(os.path.join(HERE, "station_roster.csv"))
    csv_report = Reconstruction(csv_jobs, csv_roster).classify()
    print("\n从 CSV 重新加载同一份日志，三分类结果一致: %s"
          % (csv_report["ambiguous"] == report["ambiguous"]))

    banner("场景二：产能不足 —— 根本排不下的时间段")
    overloaded = [
        JobLog("K1", 0, 20, "A"),
        JobLog("K2", 0, 20, "A"),
        JobLog("K3", 5, 15, "A"),
    ]
    bad_report = Reconstruction(overloaded, STATIONS).classify()
    print("排产状态: %s" % bad_report["status"])
    print("受影响的单: %s" % bad_report["unschedulable_jobs"])
    for station_type, windows in bad_report["overload_windows"].items():
        for window in windows:
            print(
                "  工位类型 %s 在 [%d,%d) 需求 %d 台、仅 %d 台，并行单: %s"
                % (
                    station_type,
                    window["begin"],
                    window["end"],
                    window["demand"],
                    window["capacity"],
                    window["jobs"],
                )
            )


if __name__ == "__main__":
    main()
