"""
Skills for the memory link graph.

Everything Addled remembers — facts, triples, recalled memories, session
summaries, journal entries, wiki pages and files on disk — is stored in its own
flat collection. This exposes the one graph that connects them, so the model can
answer "what else is this related to" and "which files is this about" instead of
treating each store as an island.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.skills.memory_links")


def _links():
    from backend.memory.links import KINDS, RELATIONS, link_store
    return link_store, KINDS, RELATIONS


def _summary_related(rows: list[dict], source: str) -> str:
    if not rows:
        return f"Nothing is linked to {source}."
    parts = []
    for row in rows[:8]:
        via = row.get("via") or {}
        hop = ""
        if row.get("depth", 1) > 1:
            hop = f" (via {via.get('from_kind')}:{via.get('from_id')})"
        parts.append(f"{row['kind']}:{row['ref_id']} "
                     f"[{via.get('rel', 'relates_to')}]{hop}")
    return f"Related to {source}: " + "; ".join(parts)


async def _memory_link(params: dict) -> dict:
    """Create a relation between any two memory items or files."""
    link_store, kinds, relations = _links()
    src_kind = str(params.get("kind") or "").strip().lower()
    dst_kind = str(params.get("target_kind") or "").strip().lower()
    src_id = params.get("id", "")
    dst_id = params.get("target_id", "")
    rel = str(params.get("relation") or "relates_to").strip().lower()
    note = str(params.get("note") or "").strip()

    for label, kind, ref in (("kind", src_kind, src_id),
                             ("target_kind", dst_kind, dst_id)):
        if kind not in kinds:
            return {"success": False,
                    "error": f"Unknown {label} '{kind}'. Valid kinds: "
                             + ", ".join(kinds)}
        if str(ref).strip() == "":
            return {"success": False,
                    "error": f"'{label}' needs to come with its id."}
    if rel not in relations:
        return {"success": False,
                "error": f"Unknown relation '{rel}'. Valid relations: "
                         + ", ".join(relations)}

    link_id = link_store.link(src_kind, src_id, rel, dst_kind, dst_id,
                              source="agent", note=note)
    if link_id is None:
        return {"success": False,
                "error": "Could not create the link (same item on both sides, "
                         "or the graph is unavailable)."}
    return {
        "success": True,
        "id": link_id,
        "edge": f"{src_kind}:{src_id} -{rel}-> {dst_kind}:{dst_id}",
        "summary": f"Linked {src_kind} {src_id} {rel} {dst_kind} {dst_id}.",
    }


async def _memory_unlink(params: dict) -> dict:
    """Remove a relation, either by its link id or by naming both ends."""
    link_store, kinds, _relations = _links()

    # "id" is an item id (as in memory_link), so the link's own id needs a name
    # of its own — otherwise "kind=fact, id=fact-1" reads as a link id.
    link_id = params.get("link_id")
    if link_id not in (None, "", 0, "0"):
        try:
            removed = link_store.unlink(int(link_id))
        except (TypeError, ValueError):
            return {"success": False,
                    "error": f"'{link_id}' is not a link id."}
        return {"success": removed,
                "summary": (f"Removed link {link_id}." if removed
                            else f"No link with id {link_id}."),
                **({} if removed else {"error": "No such link."})}

    src_kind = str(params.get("kind") or "").strip().lower()
    dst_kind = str(params.get("target_kind") or "").strip().lower()
    if src_kind not in kinds or dst_kind not in kinds:
        return {"success": False,
                "error": "Pass 'link_id', or pass kind, id, target_kind and "
                         "target_id to drop every edge between two items."}
    src_id = params.get("id", "")
    dst_id = params.get("target_id", "")
    rel = str(params.get("relation") or "").strip().lower()
    # Ids are stored normalised (a file path is case-folded and made absolute),
    # so a user-supplied id has to be normalised the same way before comparing.
    from backend.memory.links import normalise_ref
    try:
        _src_kind, src_norm = normalise_ref(src_kind, src_id)
        _dst_kind, dst_norm = normalise_ref(dst_kind, dst_id)
        rows = link_store.neighbors(src_kind, src_id, direction="out")
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": str(e)}
    matched = []
    for row in rows:
        if row["kind"] != dst_kind:
            continue
        try:
            if normalise_ref(row["kind"], row["ref_id"])[1] != dst_norm:
                continue
        except ValueError:
            continue
        if rel and row["rel"] != rel:
            continue
        matched.append(row)
    if not matched:
        return {"success": False,
                "error": "No such link between those two items."}
    for row in matched:
        link_store.unlink(row["id"])
    return {"success": True, "removed": len(matched),
            "summary": f"Removed {len(matched)} link(s)."}


async def _memory_related(params: dict) -> dict:
    """Everything connected to one item, following the graph."""
    link_store, kinds, _relations = _links()
    kind = str(params.get("kind") or "").strip().lower()
    ref_id = params.get("id", "")
    if kind not in kinds:
        return {"success": False,
                "error": f"Unknown kind '{kind}'. Valid kinds: " + ", ".join(kinds)}
    try:
        depth = max(1, min(int(params.get("depth") or 1), 4))
    except (TypeError, ValueError):
        depth = 1
    try:
        limit = max(1, min(int(params.get("limit") or 25), 200))
    except (TypeError, ValueError):
        limit = 25

    rows = link_store.related(kind, ref_id, depth=depth, limit=limit)
    source = f"{kind} {ref_id}"
    out = []
    for row in rows:
        item = dict(row)
        if item["kind"] == "file":
            import os
            item["exists"] = os.path.isfile(str(item["ref_id"]))
        out.append(item)
    return {
        "success": True,
        "source": {"kind": kind, "id": str(ref_id)},
        "depth": depth,
        "count": len(out),
        "related": out,
        "summary": _summary_related(out, source),
    }


async def _memory_files(params: dict) -> dict:
    """The files the memory graph points at, most referenced first."""
    link_store, _kinds, _relations = _links()
    try:
        limit = max(1, min(int(params.get("limit") or 50), 500))
    except (TypeError, ValueError):
        limit = 50
    only_existing = bool(params.get("existing_only"))
    rows = link_store.files(limit=limit)
    if only_existing:
        rows = [r for r in rows if r.get("exists")]
    return {
        "success": True,
        "count": len(rows),
        "files": rows,
        "summary": (f"{len(rows)} file(s) referenced by memory."
                    if rows else "No files are linked from memory yet."),
    }


async def _memory_graph(params: dict) -> dict:
    """Graph-wide statistics, and the raw edges when asked."""
    link_store, kinds, relations = _links()
    stats = link_store.stats()
    out = {
        "success": True,
        "kinds": list(kinds),
        "relations": list(relations),
        **stats,
    }
    if params.get("include_edges"):
        try:
            limit = max(1, min(int(params.get("limit") or 100), 1000))
        except (TypeError, ValueError):
            limit = 100
        out["edges"] = link_store.export(limit=limit)
    out["summary"] = (f"{stats.get('links', 0)} link(s) over "
                      f"{stats.get('items', 0)} item(s); "
                      f"{stats.get('files', 0)} file(s) referenced.")
    return out


def register(registry) -> None:
    """Attach the memory-link skills to a SkillRegistry."""
    from backend.skills.registry import SkillDefinition

    registry.register(SkillDefinition(
        "memory_link",
        "Create a relation between any two things Addled remembers: a fact, a "
        "knowledge-graph triple, a recalled memory, a session summary, a journal "
        "entry, a wiki page, or a file on disk. Kinds are 'fact', 'triple', "
        "'memory', 'summary', 'journal', 'wiki', 'file'. Relations are "
        "'relates_to', 'same_as', 'supersedes', 'contradicts', 'derived_from', "
        "'mentions', 'part_of', 'sourced_from', 'documents', 'links_to'. Use "
        "this when the user corrects or extends something they told Addled "
        "before, so the old and new items are visibly connected.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "description": "Kind of the first item."},
                "id": {"type": "string",
                       "description": "Id of the first item (a triple id, a "
                                      "journal date, a wiki slug, a file path)."},
                "target_kind": {"type": "string",
                                "description": "Kind of the second item."},
                "target_id": {"type": "string",
                              "description": "Id of the second item."},
                "relation": {"type": "string",
                             "description": "How they relate; defaults to "
                                            "relates_to."},
                "note": {"type": "string",
                         "description": "Optional short reason for the link."},
            },
            "required": ["kind", "id", "target_kind", "target_id"],
        },
        _memory_link,
        category="memory",
    ))

    registry.register(SkillDefinition(
        "memory_unlink",
        "Remove a relation from the memory graph, either by its 'link_id' (shown "
        "by memory_related) or by naming the two items to disconnect with "
        "kind/id and target_kind/target_id.",
        {
            "type": "object",
            "properties": {
                "link_id": {"type": "integer",
                            "description": "The numeric link id to remove."},
                "kind": {"type": "string",
                         "description": "Use with target_kind to drop every "
                                        "edge between two items instead."},
                "id": {"type": "string",
                       "description": "The first item's id."},
                "target_kind": {"type": "string",
                                "description": "The other item's kind."},
                "target_id": {"type": "string",
                              "description": "The other item's id."},
                "relation": {"type": "string",
                             "description": "Only remove edges with this "
                                            "relation."},
            },
        },
        _memory_unlink,
        category="memory",
    ))

    registry.register(SkillDefinition(
        "memory_related",
        "Everything connected to one remembered item: what it was derived "
        "from, what contradicts it, which files it mentions, which wiki pages "
        "document it. Set depth to 2 or 3 to walk outwards through the graph. "
        "Use this before answering a question about past context, and when the "
        "user asks what Addled knows about a topic or where it learned it.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "description": "fact, triple, memory, summary, "
                                        "journal, wiki or file."},
                "id": {"type": "string", "description": "The item's id."},
                "depth": {"type": "integer",
                          "description": "Hops to follow, 1-4. Default 1."},
                "limit": {"type": "integer",
                          "description": "Maximum items to return."},
            },
            "required": ["kind", "id"],
        },
        _memory_related,
        category="memory",
    ))

    registry.register(SkillDefinition(
        "memory_files",
        "The files on disk that Addled's memory refers to, with how often each "
        "is referenced and whether it still exists. Use to answer 'which files "
        "is this project about' or to find notes Addled has forgotten the path "
        "of. Set existing_only to True to hide files that have been deleted or "
        "moved.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "Maximum files to list. Default 50."},
                "existing_only": {
                    "type": "boolean",
                    "description": "Only list files that still exist."},
            },
        },
        _memory_files,
        category="memory",
    ))

    registry.register(SkillDefinition(
        "memory_graph",
        "Summary of the memory relation graph: how many links, which item "
        "kinds are connected, how many files are referenced, and which "
        "relations are in use. Set include_edges to inspect the raw edges. Use "
        "for 'how connected is my memory' or to sanity-check that relations are "
        "being recorded.",
        {
            "type": "object",
            "properties": {
                "include_edges": {
                    "type": "boolean",
                    "description": "Include the raw edge list."},
                "limit": {"type": "integer",
                          "description": "Maximum edges when included."},
            },
        },
        _memory_graph,
        category="memory",
    ))
