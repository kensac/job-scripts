"""Who sees which posting, and on what grounds.

`visibility` is the one predicate, computed into board_visible and read as a
lookup. `eligibility` is the structural half it shares with the sweeps,
`criteria` the person's own date and location bounds, and `access` the
per-object check a route makes before serving one.

Grouped because answering "why can this person not see that posting" used to
mean knowing which of four flat modules held the clause, and answering it
wrongly is the defect this migration started from.
"""
