"""LLM Wiki: an interlinked markdown knowledge base Addled maintains.

Karpathy's pattern, implemented natively: instead of retrieving from scratch on
every question, Addled incrementally builds and maintains a persistent wiki of
interlinked pages from the user's sources. The pages are plain markdown the user
owns and can read, edit or delete.

Wiki ``[[links]]`` and page ``sources`` are mirrored into ``memory/links.db``, so
the wiki's own link graph is the same graph the rest of the memory system uses.
"""

from backend.wiki import ingest, query, store  # noqa: F401

__all__ = ["ingest", "query", "store"]
