"""Verify handover-form succession claims against sqlite-history trigger records."""
import sqlite3

DB_PATH = "audit/repairs.db"
COLS = ("biz_no", "model", "status")
BITS = {"biz_no": 1, "model": 2, "status": 4}
ALL_MASK = 2 ** len(COLS) - 1


def load_history(conn):
    rows = conn.execute(
        "SELECT rowid, _rowid, biz_no, model, status, _version, _updated, _mask "
        "FROM _devices_history ORDER BY _updated, rowid"
    ).fetchall()
    chains = {}
    for hist_rowid, rowid, biz_no, model, status, version, updated, mask in rows:
        chains.setdefault(rowid, []).append(
            {
                "hist_rowid": hist_rowid,
                "version": version,
                "updated": updated,
                "mask": mask,
                "biz_no": biz_no,
                "model": model,
                "status": status,
            }
        )
    return chains


def split_segments(events):
    """Split one physical rowid's events at delete/insert lifecycle boundaries."""
    segments = []
    current = []
    for ev in events:
        starts_new = ev["mask"] == ALL_MASK and (not current or current[-1]["mask"] == -1)
        if starts_new and current:
            segments.append(current)
            current = []
        current.append(ev)
        if ev["mask"] == -1:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return segments


def reconstruct(chains):
    """Replay every rowid segment into full-state snapshots."""
    timeline = {}
    for rowid, events in chains.items():
        segment_records = []
        for seg_index, segment in enumerate(split_segments(events), start=1):
            state = None
            snaps = []
            for ev in segment:
                mask = ev["mask"]
                if state is None:
                    state = {"biz_no": ev["biz_no"], "model": ev["model"], "status": ev["status"]}
                    snaps.append(("INSERT", ev["version"], dict(state), mask, [], seg_index))
                    continue
                if mask == -1:
                    snaps.append(("DELETE", ev["version"], dict(state), mask, [], seg_index))
                    state = None
                    continue
                changed = [name for name, bit in BITS.items() if mask & bit]
                state = dict(state)
                for name in changed:
                    state[name] = ev[name]
                snaps.append(("UPDATE", ev["version"], dict(state), mask, changed, seg_index))
            segment_records.append(snaps)
        timeline[rowid] = segment_records
    return timeline


def biz_no_locations(timeline):
    """Map each biz_no to (rowid, segment, version, kind) points where it appears."""
    locations = {}
    for rowid, segments in timeline.items():
        for snaps in segments:
            for kind, version, state, mask, changed, seg_index in snaps:
                locations.setdefault(state["biz_no"], []).append(
                    (rowid, seg_index, version, kind)
                )
    return locations


def verify_forms(conn, timeline, locations):
    forms = conn.execute(
        "SELECT form_id, predecessor_no, successor_no, issued_at "
        "FROM handover_forms ORDER BY issued_at, form_id"
    ).fetchall()
    claimants = {}
    for form_id, _, succ, _ in forms:
        claimants.setdefault(succ, []).append(form_id)

    results = []
    for form_id, pred, succ, issued_at in forms:
        verdict, evidence, gaps = adjudicate(pred, succ, timeline, locations)
        if len(claimants.get(succ, [])) > 1:
            gaps.append(
                "后继 %s 被 %d 张交接单抢认（%s）；不能任选其一串链，两边均维持未定"
                % (succ, len(claimants[succ]), "、".join(claimants[succ]))
            )
            if verdict == "确认":
                verdict = "未定"
        results.append(
            {
                "form_id": form_id,
                "predecessor": pred,
                "successor": succ,
                "issued_at": issued_at,
                "verdict": verdict,
                "evidence": evidence,
                "gaps": gaps,
            }
        )
    return results


def adjudicate(pred, succ, timeline, locations):
    pred_points = locations.get(pred, [])
    succ_points = locations.get(succ, [])
    if not pred_points:
        return "未定", [], ["触发器记录中找不到前驱业务号 %s 的任何痕迹" % pred]
    if not succ_points:
        return "未定", [], ["触发器记录中找不到后继业务号 %s 的任何痕迹" % succ]

    pred_rowids = sorted({r for r, _, _, _ in pred_points})
    succ_rowids = sorted({r for r, _, _, _ in succ_points})
    shared = sorted(set(succ_rowids) & set(pred_rowids))

    if shared:
        for rowid in shared:
            pred_here = [(s, v) for r, s, v, k in pred_points if r == rowid]
            succ_here = [(s, v) for r, s, v, k in succ_points if r == rowid]
            pred_segments = {s for s, _ in pred_here}
            succ_segments = {s for s, _ in succ_here}
            if pred_segments == succ_segments and max(v for _, v in pred_here) < min(
                v for _, v in succ_here
            ):
                evidence = [
                    "rowid %d 同一生命周期内：%s（v%d 止）主键改写为 %s（v%d 起），中间无 DELETE"
                    % (
                        rowid,
                        pred,
                        max(v for _, v in pred_here),
                        succ,
                        min(v for _, v in succ_here),
                    )
                ]
                return "确认", evidence, []
            if max(pred_segments) < min(succ_segments):
                evidence = [
                    "rowid %d 第 %d 段生命周期以 %s 的 DELETE（mask=-1）终结；第 %d 段以 %s 的新 INSERT 开始"
                    % (rowid, max(pred_segments), pred, min(succ_segments), succ)
                ]
                gaps = [
                    "两段生命周期之间物理行链已断裂，新 INSERT 的 _version 从 1 重新计数，与旧段无版本衔接",
                    "rowid 被复用只能证明同槽位先删后建，不能证明是同一台设备，承接仅为交接单单方主张",
                ]
                return "未定", evidence, gaps
        return (
            "未定",
            ["%s 与 %s 共用 rowid %s，但版本/分段顺序无法证明直接承接" % (pred, succ, shared)],
            ["同一 rowid 上记录交错，缺少干净的一前一后改写证据"],
        )

    evidence = [
        "后继 %s 只出现在 rowid %s；前驱 %s 只出现在 rowid %s，二者从未共享物理行"
        % (succ, succ_rowids, pred, pred_rowids)
    ]
    alive = []
    for rowid in pred_rowids:
        last_snaps = timeline[rowid][-1]
        last = last_snaps[-1]
        if last[0] != "DELETE":
            alive.append("rowid %d 现号 %s" % (rowid, last[2]["biz_no"]))
    gaps = ["触发器无法在新物理行与前驱之间建立血缘"]
    if alive:
        gaps.append("前驱物理行仍在册未注销（%s），不存在换号承接的客观条件" % "；".join(alive))
    return "未定", evidence, gaps


def print_timeline(timeline):
    print("=" * 80)
    print("一、按物理 rowid + 生命周期分段重建的设备完整履历")
    print("=" * 80)
    for rowid in sorted(timeline):
        print("\n物理 rowid %d" % rowid)
        for snaps in timeline[rowid]:
            seg = snaps[0][5]
            print("  -- 生命周期段 %d --" % seg)
            for kind, version, state, mask, changed, _ in snaps:
                if kind == "UPDATE":
                    extra = " 改动列=%s mask=%d" % (",".join(changed), mask)
                elif kind == "INSERT":
                    extra = " mask=%d（插入全量，_version 恒为 1）" % mask
                else:
                    extra = " mask=-1（删除，留存删除前全量）"
                print(
                    "    v%-2d %-6s biz_no=%-5s model=%-4s status=%-6s%s"
                    % (version, kind, state["biz_no"], state["model"], state["status"], extra)
                )


def print_forms(results):
    print("\n" + "=" * 80)
    print("二、交接单声称的承接关系逐条裁定")
    print("=" * 80)
    for item in results:
        print(
            "\n[%s] %s -> %s（%s 签发）裁定：%s"
            % (item["form_id"], item["predecessor"], item["successor"], item["issued_at"], item["verdict"])
        )
        for line in item["evidence"]:
            print("  证据：%s" % line)
        for line in item["gaps"]:
            print("  缺口：%s" % line)


def print_control(locations, timeline, results):
    print("\n" + "=" * 80)
    print("三、未涉争议设备与临时号的可追查性")
    print("=" * 80)
    claimed = set()
    for item in results:
        claimed.add(item["predecessor"])
        claimed.add(item["successor"])
    current_devices = {}
    for rowid, segments in timeline.items():
        last = segments[-1][-1]
        if last[0] != "DELETE":
            current_devices[last[2]["biz_no"]] = (rowid, len(segments))
    for biz_no in sorted(current_devices):
        rowid, seg_count = current_devices[biz_no]
        if biz_no not in claimed:
            print(
                "  %s：现占 rowid %d（%d 段生命周期），无交接单争议，履历独立可追查"
                % (biz_no, rowid, seg_count)
            )
    transient = sorted(
        biz_no
        for biz_no, points in locations.items()
        if biz_no not in current_devices and all(kind == "UPDATE" for _, _, _, kind in points)
    )
    for biz_no in transient:
        rowids = sorted({r for r, _, _, _ in locations[biz_no]})
        print("  %s：仅为交换过程中的临时业务号，出现在 rowid %s 的中间版本，非在册设备" % (biz_no, rowids))


def analyze(db_path):
    conn = sqlite3.connect(db_path)
    try:
        chains = load_history(conn)
        timeline = reconstruct(chains)
        locations = biz_no_locations(timeline)
        results = verify_forms(conn, timeline, locations)
    finally:
        conn.close()
    return timeline, locations, results


def main():
    timeline, locations, results = analyze(DB_PATH)
    print_timeline(timeline)
    print_forms(results)
    print_control(locations, timeline, results)


if __name__ == "__main__":
    main()
