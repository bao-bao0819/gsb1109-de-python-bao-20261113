"""Command line entry point: ``python -m translation_audit dataset.json``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from translation_audit.audit import AuditResult
from translation_audit.audit import audit_batch
from translation_audit.audit import describe_box
from translation_audit.audit import load_dataset


def render_report(result: AuditResult) -> str:
    lines: list[str] = []
    if result.common_translation_possible:
        lines.append("VERDICT: a common translation CAN explain the whole batch")
        lines.append("Allowed translation range(s):")
        for box in result.allowed_translation_boxes:
            lines.append(f"  - {describe_box(box)}")
    else:
        lines.append("VERDICT: NO common translation can explain the whole batch")

    lines.append("")
    lines.append("Control-vertex evidence (per-pair displacement boxes):")
    for control in result.controls:
        lines.append(
            f"  - {control.label} [{control.source}]: {describe_box(control.box)}"
        )

    lines.append("")
    if result.unique_matches:
        lines.append("Uniquely matched outlines:")
        for old_id, new_id in sorted(result.unique_matches.items()):
            lines.append(f"  - {old_id} -> {new_id}")
    if result.ambiguous_matches:
        lines.append("Outlines still admitting multiple correspondences:")
        for old_id, targets in sorted(result.ambiguous_matches.items()):
            lines.append(f"  - {old_id} -> {', '.join(targets)}")
    if result.unmatched:
        lines.append("Outlines without a feasible one-to-one assignment:")
        for name, reasons in sorted(result.unmatched.items()):
            lines.append(f"  - {name}: {'; '.join(reasons)}")

    if result.feasible_matchings and not result.common_translation_possible:
        lines.append("")
        count = len(result.feasible_matchings)
        lines.append(f"Group-local feasible assignments checked: {count}")

    if result.conflicts:
        lines.append("")
        lines.append("Conflicting control evidence for the surveyor:")
        for conflict in result.conflicts:
            lines.append(f"  - [{conflict['type']}] {conflict['detail']}")
            for evidence in conflict.get("evidence", []):
                if isinstance(evidence, dict):
                    box = tuple(evidence["box"])
                    lines.append(
                        f"      * {evidence['label']} ({evidence['source']}): "
                        f"{describe_box(box)}"
                    )
                else:
                    lines.append(f"      * {evidence}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dataset",
        type=Path,
        help="JSON dataset (old/new outlines + tolerance)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON",
    )
    args = parser.parse_args()

    dataset = load_dataset(args.dataset)
    result = audit_batch(dataset)
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
