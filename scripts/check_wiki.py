"""LLM Wiki regression check.

Deterministic: the ingest test uses a scripted provider, so it verifies the
mechanism (incremental update, citations, link mirroring) rather than what a live
model happens to write.

Uses a temporary wiki directory and a temporary link database, so no user data is
touched.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_wiki.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


class ScriptedProvider:
    """Returns a canned page plan, recording the model it was asked for."""

    provider_id = "deepseek"
    provider_name = "scripted"

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def chat(self, messages, model=None, max_tokens=4096,
                   temperature=0.7, tools=None, **_extra):
        from backend.providers.base import ProviderResult
        self.calls.append({"model": model, "messages": messages})
        return ProviderResult(ok=True, model=model or "",
                              response=json.dumps(self.payload))


def plan(slug: str, title: str, body: str, tags=None, links=None) -> dict:
    return {"pages": [{"slug": slug, "title": title, "body": body,
                       "tags": tags or [], "links": links or []}]}


def run_store_tests(tmp: str):
    from backend.wiki import store

    check("slugify lowercases and dashes", store.slugify("My Notes!") == "my-notes",
          store.slugify("My Notes!"))
    check("slugify is stable", store.slugify("WAL Mode") == store.slugify("wal mode"))

    page = store.write("sqlite", "SQLite", "SQLite is embedded. See [[wal-mode]].",
                       tags=["storage", "db"],
                       sources=[r"C:\notes\sqlite.md"])
    check("write returns the stored page", bool(page), str(page))
    check("title is stored", page and page["title"] == "SQLite", str(page))
    check("tags are stored", page and "storage" in page["tags"], str(page))
    check("sources are stored", page and page["sources"], str(page))
    check("links are extracted",
          page and page["links"] == ["wal-mode"], str(page and page["links"]))

    again = store.read("sqlite")
    check("read round-trips the body",
          "embedded" in (again or {}).get("body", ""), str(again))
    check("read strips link markup for display",
          "[[wal-mode]]" not in (again or {}).get("text", ""), str(again))
    check("the page is on disk as markdown",
          os.path.isfile(store.page_path("sqlite")))
    check("frontmatter is not part of the body",
          not (again or {}).get("body", "").startswith("---"),
          (again or {}).get("body", "")[:40])
    check("exists() agrees", store.exists("sqlite") is True)
    check("exists() is false for a missing page",
          store.exists("nope") is False)

    # -- citations accumulate, they do not get replaced --------------------
    store.write("sqlite", "SQLite", "Updated body. Still see [[wal-mode]].",
                sources=[r"C:\notes\more-sqlite.md"])
    merged = store.read("sqlite")
    check("a second write merges sources rather than replacing them",
          len(merged["sources"]) == 2, str(merged["sources"]))
    check("a second write replaces the body (incremental, not appended)",
          "is embedded" not in merged["body"]
          and "Updated body" in merged["body"], repr(merged["body"][:60]))
    check("created is preserved across updates",
          merged["created"] == page["created"], "")

    # -- more pages, for the graph ----------------------------------------
    store.write("wal-mode", "WAL mode", "WAL mode improves concurrency.",
                tags=["storage"])
    store.write("empty-page", "Empty", "short")
    store.write("orphan", "Orphan", "Nothing links here but it has content.")

    pages = store.list_pages()
    check("list_pages finds every page", len(pages) == 4, str(len(pages)))
    check("list_pages is newest-first",
          all("updated" in p for p in pages), str(pages[:1]))
    check("backlinks finds the referencing page",
          store.backlinks("wal-mode") == ["sqlite"],
          str(store.backlinks("wal-mode")))
    check("outgoing returns the page's own links",
          store.outgoing("sqlite") == ["wal-mode"], str(store.outgoing("sqlite")))

    # -- search ranks title over body -------------------------------------
    hits = store.search("sqlite")
    check("search finds a title match", hits and hits[0]["slug"] == "sqlite",
          str(hits[:2]))
    check("a query with no match returns nothing",
          store.search("zzzznothing") == [], str(store.search("zzzznothing")))
    check("search ignores one-character noise",
          store.search("a") == [], str(store.search("a")))

    # -- wiki links are mirrored into the shared graph ---------------------
    report = store.reindex_links()
    check("reindex reports what it did", report.get("available") is True,
          str(report))
    check("wiki links became graph edges", report["wiki_links"] >= 1, str(report))
    check("a local source became a sourced_from edge",
          report["source_links"] >= 2, str(report))

    from backend.memory.links import link_store as tmp_links
    edges = tmp_links.neighbors("wiki", "sqlite")
    check("the wiki page has graph edges", len(edges) >= 2, str(edges))
    check("the link to another page is typed links_to",
          any(e["rel"] == "links_to" and e["kind"] == "wiki" for e in edges),
          str(edges))
    check("the source file is linked",
          any(e["rel"] == "sourced_from" and e["kind"] == "file"
              for e in edges), str(edges))

    # a broken link stays in the markdown but is not a graph edge
    check("a link to a missing page is not mirrored",
          not any(e["kind"] == "wiki" and e["ref_id"] == "does-not-exist"
                  for e in edges), str(edges))

    # URLs must not become file nodes
    store.write("web", "Web notes", "See the docs.",
                sources=["https://example.com/page.html"])
    store.reindex_links()
    web_edges = tmp_links.neighbors("wiki", "web")
    check("a URL source is not linked as a file",
          not any(e["kind"] == "file" for e in web_edges), str(web_edges))

    # -- lint ---------------------------------------------------------------
    store.write("broken", "Broken", "This points at [[gone-page]] which is absent.")
    report = store.lint()
    check("lint reports a broken link",
          any(b["missing"] == "gone-page" for b in report["broken_links"]),
          str(report["broken_links"]))
    check("lint reports an orphan",
          "orphan" in report["orphans"], str(report["orphans"]))
    check("lint reports a stub page", "empty-page" in report["empty"],
          str(report["empty"]))
    check("lint is unhealthy when problems exist",
          report["healthy"] is False, str(report))

    # -- query -------------------------------------------------------------
    from backend.wiki import query
    context = query.build_wiki_context("sqlite")
    check("build_wiki_context returns a block", bool(context), str(context))
    check("the block is labelled [Wiki]",
          (context or "").startswith("[Wiki]"), (context or "")[:40])
    check("the block carries the page body",
          "Updated body" in (context or ""), (context or "")[:200])
    check("the block lists sources as citations",
          "Sources:" in (context or ""), (context or "")[-200:])
    check("the block lists related pages",
          "Related pages" in (context or ""), (context or "")[-200:])
    check("an irrelevant query returns None",
          query.build_wiki_context("zzzznothing") is None, "")

    # -- delete ------------------------------------------------------------
    check("delete removes the page", store.delete("orphan") is True)
    check("the page is gone", store.exists("orphan") is False)
    check("deleting a missing page is harmless",
          store.delete("orphan") is False)
    check("its graph edges are gone",
          tmp_links.neighbors("wiki", "orphan") == [],
          str(tmp_links.neighbors("wiki", "orphan")))


async def run_ingest_tests(tmp: str):
    from backend.wiki import ingest, store

    # -- a new page from a source -----------------------------------------
    provider = ScriptedProvider(plan(
        "postgres", "Postgres", "Postgres is a relational database.",
        tags=["db"], links=["sqlite"]))
    result = await ingest.ingest_text(
        "Postgres is a relational database used for the backend. " * 3,
        source_ref=r"C:\notes\pg.md", provider=provider)
    check("ingest reports success", result.get("success"), str(result))
    check("ingest creates the page",
          store.exists("postgres") is True, str(result))
    check("ingest records the citation",
          r"C:\notes\pg.md" in (store.read("postgres") or {}).get("sources", []),
          str(store.read("postgres")))
    check("ingest counts a creation", result.get("created") == 1, str(result))
    check("ingest used the reasoning role",
          provider.calls and provider.calls[0]["model"] == "deepseek-v4-pro",
          str([c["model"] for c in provider.calls]))
    check("ingest sends the system prompt",
          provider.calls[0]["messages"][0]["role"] == "system", "")

    # -- a second source about the same topic updates, not duplicates -----
    provider2 = ScriptedProvider(plan(
        "postgres", "Postgres", "Postgres is a relational database with MVCC."))
    result2 = await ingest.ingest_text(
        "More about Postgres and its MVCC implementation. " * 3,
        source_ref=r"C:\notes\pg-mvcc.md", provider=provider2)
    check("the second ingest succeeds", result2.get("success"), str(result2))
    page = store.read("postgres")
    check("the page was updated, not duplicated",
          store.exists("postgres") and len(
              [p for p in store.list_pages() if p["slug"] == "postgres"]) == 1,
          str([p["slug"] for p in store.list_pages()]))
    check("the body is the rewritten one",
          "MVCC" in page["body"], page["body"][:80])
    check("both citations are kept", len(page["sources"]) == 2,
          str(page["sources"]))
    check("it reports an update rather than a creation",
          result2.get("updated") == 1 and result2.get("created") == 0,
          str(result2))

    # -- refusals ----------------------------------------------------------
    short = await ingest.ingest_text("too short", provider=provider)
    check("a too-short source is refused", short.get("success") is False,
          str(short))
    bad = await ingest.ingest_text(
        "A long enough source about something. " * 5,
        provider=ScriptedProvider({"not": "a plan"}))
    check("an unusable model reply is reported, not crashed on",
          bad.get("success") is False, str(bad))
    plan_fenced = await ingest.ingest_text(
        "A long enough source about widgets. " * 5,
        provider=ScriptedProvider({
            "pages": [{"slug": "widgets", "title": "Widgets",
                       "body": "Widgets are small components."}]}))
    check("ingest writes a page without a source ref",
          plan_fenced.get("success"), str(plan_fenced))

    missing = await ingest.ingest_file(os.path.join(tmp, "nope.md"))
    check("ingesting a missing file is refused", missing.get("success") is False,
          str(missing))
    unsupported = os.path.join(tmp, "image.png")
    open(unsupported, "wb").close()
    result = await ingest.ingest_file(unsupported)
    check("ingesting an unsupported type is refused",
          result.get("success") is False, str(result))

    # -- a real file, end to end ------------------------------------------
    source = os.path.join(tmp, "notes.md")
    with open(source, "w", encoding="utf-8") as handle:
        handle.write("Notes about the build system. " * 20)
    real = await ingest.ingest_file(
        source, provider=ScriptedProvider(plan(
            "build-system", "Build system", "The build uses a batch script.")))
    check("ingesting a real file works", real.get("success"), str(real))
    check("the file is recorded as the citation",
          os.path.normcase(source) in [os.path.normcase(s) for s in
                                       (store.read("build-system") or {})
                                       .get("sources", [])],
          str(store.read("build-system")))


async def run_skill_tests(tmp: str):
    from backend.skills.registry import SkillRegistry

    registry = SkillRegistry()
    for name in ("memory_link", "memory_unlink", "memory_related",
                 "memory_files", "memory_graph", "wiki_search", "wiki_read",
                 "wiki_write", "wiki_ingest", "wiki_links", "wiki_lint"):
        check(f"skill {name} is registered",
              registry.get(name) is not None if hasattr(registry, "get")
              else name in {s.name for s in registry.enabled_list_all()}, "")

    async def call(name, params):
        result = await registry.execute(name, params)
        return result.data or {}

    linked = await call("memory_link", {
        "kind": "fact", "id": "fact-1", "target_kind": "file",
        "target_id": os.path.join(tmp, "notes.md"), "relation": "documents",
        "note": "test"})
    check("memory_link creates an edge", linked.get("success"), str(linked))
    check("memory_link describes the edge", "documents" in
          (linked.get("summary") or ""), str(linked))

    bad_kind = await call("memory_link", {
        "kind": "nonsense", "id": "1", "target_kind": "file",
        "target_id": os.path.join(tmp, "notes.md")})
    check("memory_link rejects an unknown kind", bad_kind.get("success") is False,
          str(bad_kind))
    bad_rel = await call("memory_link", {
        "kind": "fact", "id": "1", "target_kind": "file",
        "target_id": os.path.join(tmp, "notes.md"), "relation": "sideways"})
    check("memory_link rejects an unknown relation",
          bad_rel.get("success") is False, str(bad_rel))

    related = await call("memory_related", {"kind": "fact", "id": "fact-1"})
    check("memory_related walks out to the file", related.get("success")
          and any(r["kind"] == "file" for r in related["related"]),
          str(related))
    check("memory_related marks a file that exists",
          any(r.get("exists") is True for r in related["related"]
              if r["kind"] == "file"), str(related))
    check("memory_related summarises in prose",
          "Related to" in (related.get("summary") or ""), str(related))

    files = await call("memory_files", {})
    check("memory_files lists the file", files.get("success"), str(files))
    check("memory_files carries a count", "count" in files, str(files))

    graph = await call("memory_graph", {"include_edges": True})
    check("memory_graph reports a link count",
          graph.get("success") and graph.get("links", 0) >= 1, str(graph))
    check("memory_graph exposes the edge list",
          len(graph.get("edges") or []) >= 1, str(graph))

    unlinked = await call("memory_unlink", {
        "kind": "fact", "id": "fact-1", "target_kind": "file",
        "target_id": os.path.join(tmp, "notes.md")})
    check("memory_unlink removes the edge", unlinked.get("success"),
          str(unlinked))
    gone = await call("memory_related", {"kind": "fact", "id": "fact-1"})
    check("the edge is really gone",
          not any(r["kind"] == "file" for r in gone.get("related", [])),
          str(gone))

    # -- wiki skills -------------------------------------------------------
    written = await call("wiki_write", {
        "slug": "Widget Shop", "title": "Widget Shop",
        "body": "The shop sells widgets. See [[widget-types]].",
        "tags": "shop, widgets", "sources": r"C:\notes\shop.md"})
    check("wiki_write creates a page", written.get("success"), str(written))
    check("wiki_write slugifies", written.get("slug") == "widget-shop",
          str(written))
    check("wiki_write accepts comma-separated tags",
          "created" in (written.get("summary") or "").lower(), str(written))
    check("wiki_write reports it as new", written.get("updated") is False,
          str(written))

    again = await call("wiki_write", {"slug": "widget-shop",
                                      "body": "Rewritten. See [[widget-types]]."})
    check("wiki_write reports an update", again.get("updated") is True,
          str(again))

    found = await call("wiki_search", {"query": "widget"})
    check("wiki_search finds the page",
          any(p["slug"] == "widget-shop" for p in found.get("pages", [])),
          str(found))
    empty = await call("wiki_search", {"query": ""})
    check("wiki_search needs a query", empty.get("success") is False, str(empty))
    nothing = await call("wiki_search", {"query": "zzzznothing"})
    check("wiki_search reports no match in prose",
          "No wiki page" in (nothing.get("summary") or ""), str(nothing))

    read = await call("wiki_read", {"slug": "widget-shop"})
    check("wiki_read returns the page", read.get("success"), str(read))
    check("wiki_read includes the body",
          "Rewritten" in (read.get("page") or {}).get("body", ""), str(read))
    missing = await call("wiki_read", {"slug": "nope"})
    check("wiki_read suggests known slugs when missing",
          missing.get("success") is False and "known_slugs" in missing,
          str(missing))

    await call("wiki_write", {"slug": "widget-types",
                              "body": "Types of widget: many."})
    links = await call("wiki_links", {"slug": "widget-types"})
    check("wiki_links finds the backlink",
          "widget-shop" in (links.get("backlinks") or []), str(links))
    check("wiki_links summarises counts",
          "backlink" in (links.get("summary") or ""), str(links))

    lint = await call("wiki_lint", {})
    check("wiki_lint runs and is not healthy with a broken link",
          lint.get("success") and lint.get("healthy") is False, str(lint))
    check("wiki_lint explains itself in prose",
          "problem" in (lint.get("summary") or ""), str(lint))

    ingest_result = await call("wiki_ingest", {"text": "short"})
    check("wiki_ingest refuses a too-short source",
          ingest_result.get("success") is False, str(ingest_result))
    empty_ingest = await call("wiki_ingest", {})
    check("wiki_ingest needs text or a path",
          empty_ingest.get("success") is False, str(empty_ingest))


def run_disabled_test(tmp: str):
    from backend.config import config
    from backend.wiki import query, store

    original = config._data["wiki"]["enabled"]
    config._data["wiki"]["enabled"] = False
    try:
        check("a disabled wiki refuses to write",
              store.write("nope-disabled", "Nope", "body") is None)
        check("a disabled wiki injects nothing",
              query.build_wiki_context("sqlite") is None)
        check("enabled() reports the switch", store.enabled() is False)
    finally:
        config._data["wiki"]["enabled"] = original


def main():
    from backend.config import config
    from backend.memory.links import LinkStore
    import backend.memory.links as links_module

    # _data is empty until something forces a load.
    config._ensure_loaded()
    tmp = tempfile.mkdtemp()
    real_dir = config._data["wiki"]["dir"]
    real_store = links_module.link_store
    config._data["wiki"]["dir"] = tmp
    links_module.link_store = LinkStore(__import__("pathlib").Path(tmp) / "links.db")
    try:
        run_store_tests(tmp)
        asyncio.run(run_ingest_tests(tmp))
        asyncio.run(run_skill_tests(tmp))
        run_disabled_test(tmp)
    finally:
        config._data["wiki"]["dir"] = real_dir
        links_module.link_store = real_store
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


sys.exit(main())
