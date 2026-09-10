"""Project awareness — index the user's code workspace for chat retrieval."""

from backend.project.indexer import index_roots, search_project, status  # noqa: F401
from backend.project.patterns import detect_patterns, maybe_suggest_patterns  # noqa: F401
