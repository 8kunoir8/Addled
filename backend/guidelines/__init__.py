"""Guideline packs: third-party rulesets Addled can follow.

Bundled behaviour lives in ``backend/skills/registry.py``; these are external
prompt/ruleset *documents* (ponytail, the Karpathy coding guidelines) that are
fetched from their upstream repos, cached locally, and injected into the system
prompt for the tasks they apply to. Nothing is vendored into this repository, so
upstream edits arrive on the next refresh.
"""

from backend.guidelines import inject, packs, store  # noqa: F401

__all__ = ["inject", "packs", "store"]
