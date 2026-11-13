"""
Reproducible sample data (可复现小样) for the catalog verification.

The sample contains two clusters:

* Parcels A, B, C form a clean tiling.  B's outer ring is given
  clockwise (violating the convention) and carries an extra collinear
  vertex ``(2, 1)`` on the edge shared with A.  C's hole is given
  counter-clockwise (a hole must run opposite to its outer ring).
* Parcels X, Y, Z overlap.  On the segment ``(12, 0) -> (13, 0)`` all
  three of them claim the left side, which is the shortest edge segment
  with three same-side claimants.
"""

from left_right_catalog.core import Parcel

SAMPLE_PARCELS: tuple[Parcel, ...] = (
    Parcel("A", outer=((0, 0), (2, 0), (2, 2), (0, 2))),
    # Given clockwise (to be flipped) with extra collinear vertex (2, 1).
    Parcel("B", outer=((2, 0), (2, 1), (2, 2), (4, 2), (4, 0))),
    # The hole is given counter-clockwise (to be flipped to clockwise).
    Parcel(
        "C",
        outer=((0, 2), (4, 2), (4, 5), (0, 5)),
        holes=(((1, 3), (3, 3), (3, 4), (1, 4)),),
    ),
    Parcel("X", outer=((10, 0), (14, 0), (14, 2), (10, 2))),
    Parcel("Y", outer=((11, 0), (15, 0), (15, 2), (11, 2))),
    Parcel("Z", outer=((12, 0), (13, 0), (13, 3), (12, 3))),
)

SAMPLE_QUERIES: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = (
    ((2, 0), (2, 2)),
    ((2, 2), (2, 0)),
    ((0, 2), (4, 2)),
)
