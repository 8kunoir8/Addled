"""Standing permission for skills and tools — see `policy.py`.

Its own package rather than a module beside `actions/` because both the action
executor and the skill registry import it, and neither should pull in the
other to reach it.
"""
