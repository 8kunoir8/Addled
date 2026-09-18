"""
Standard operating procedures.

The task category says which recipe applies; the recipe is what worked last
time. :mod:`store` keeps them, :mod:`match` finds the one that fits,
:mod:`learn` adds to them from runs that succeeded, and :mod:`seeds` provides
the few that are worth having from the start.
"""

from backend.sop import learn, match, seeds, store  # noqa: F401
