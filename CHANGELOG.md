# Unreleased

 * Optional barrier lines and sampling valid regions for IDW elevation queries
 * Support points whose path to the query point crosses a barrier (or that lie
   on a barrier, including its endpoints) no longer contribute; endpoint
   grazing and query-point-on-barrier cases have explicit semantics
 * Barriers use global (lat, lon) coordinates and can span SRTM file boundaries
 * `_IDW_detailed` returns the elevation together with every excluded support
   point, nearest first, and the reason it was rejected (`void`,
   `outside_valid_region` or `blocked_by_barrier[i].segment[j]`); the
   elevation is `None` when valid support is insufficient

# 0.3.2

 * PIL import fix
 * requests timeout when fetching SRTM files

# 0.3.1

 * Missing requests dependency, use pyflakes, minor fixes

# ...
