"""Demo entry point: ``python -m left_right_catalog``."""

from left_right_catalog.report import format_report
from left_right_catalog.sample import SAMPLE_PARCELS
from left_right_catalog.sample import SAMPLE_QUERIES


def main() -> None:
    """Print the verification report for the reproducible sample."""
    print(format_report(SAMPLE_PARCELS, SAMPLE_QUERIES))  # noqa: T201


if __name__ == "__main__":
    main()
