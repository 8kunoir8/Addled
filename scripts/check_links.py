"""Memory relations regression check.

Covers the link layer (edges, traversal, cleanup, integrity) and automatic
linking (file paths in memory text, write-time provenance).

The store tests run against a temporary database; the hook test backs up and
restores ``facts.json`` so no user data is left behind.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_links.py
"""

import asyncio
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


def run_store_tests(tmp: str):
    from backend.memory import autolink
    from backend.memory.links import LinkStore, normalise_ref

    store = LinkStore(__import__("pathlib").Path(tmp) / "links.db")
    check("store opens", store.available is True)

    # -- basic edge, addressed from both sides -----------------------------
    import pathlib
    target = pathlib.Path(tmp) / "notes.md"
    target.write_text("hello", encoding="utf-8")

    store.link("fact", 1, "mentions", "file", str(target), note="a fact")
    edges = store.neighbors("fact", 1)
    check("fact sees the file", len(edges) == 1 and edges[0]["kind"] == "file",
          str(edges))
    check("edge carries the relation", edges[0]["rel"] == "mentions",
          str(edges[0].get("rel")))
    check("edge is rendered as outgoing", edges[0]["direction"] == "out",
          str(edges[0].get("direction")))

    back = store.neighbors("file", str(target))
    check("the file sees the fact (reverse lookup)",
          len(back) == 1 and back[0]["kind"] == "fact"
          and back[0]["direction"] == "in", str(back))

    # -- upsert, not duplicate --------------------------------------------
    store.link("fact", 1, "mentions", "file", str(target), note="again")
    check("re-linking upserts instead of duplicating",
          store.stats()["links"] == 1, str(store.stats()))
    check("the note is refreshed",
          store.neighbors("fact", 1)[0]["note"] == "again")

    # -- file normalisation -------------------------------------------------
    store.link("fact", 2, "mentions", "file", str(target).upper())
    check("one file mentioned two ways is one node",
          store.stats()["links"] == 2, str(store.stats()))
    check("normalised to a single file target",
          len(store.files()) == 1, str(store.files()))

    # -- rejections ---------------------------------------------------------
    check("self-loops are refused",
          store.link("fact", 3, "relates_to", "fact", 3) is None)
    check("an unknown kind is refused",
          store.link("nonsense", 1, "relates_to", "fact", 1) is None)
    check("an empty id is refused",
          store.link("fact", "  ", "relates_to", "fact", 9) is None)
    try:
        normalise_ref("nope", "1")
        check("normalise_ref rejects an unknown kind", False, "no error raised")
    except ValueError:
        check("normalise_ref rejects an unknown kind", True)
    store.link("fact", 4, "invented_relation", "fact", 5)
    check("an unknown relation falls back to relates_to",
          store.neighbors("fact", 4)[0]["rel"] == "relates_to",
          str(store.neighbors("fact", 4)))

    # -- distinct kinds are separate nodes -----------------------------
    store.link("fact", 9, "relates_to", "triple", 9)
    check("the same numeric id in two kinds is two nodes",
          store.neighbors("fact", 9)[0]["kind"] == "triple",
          str(store.neighbors("fact", 9)))
    store.link("wiki", "page-a", "mentions", "file", str(target))
    store.link("fact", 1, "documents", "wiki", "page-a")
    one = store.related("fact", 1, depth=1)
    two = store.related("fact", 1, depth=2)
    check("depth 1 reaches the direct neighbours", len(one) >= 1, str(one))
    check("depth 2 reaches further", len(two) > len(one),
          f"d1={len(one)} d2={len(two)}")
    hops = {r["kind"]: r["depth"] for r in two}
    check("the wiki page is found at depth 1", hops.get("wiki") == 1, str(hops))
    check("results explain how they were reached",
          all("via" in r and r["via"].get("rel") for r in two), str(two[:2]))
    check("traversal is deduplicated",
          len(two) == len({(r["kind"], r["ref_id"]) for r in two}))
    check("depth is clamped, not unbounded",
          len(store.related("fact", 1, depth=99)) == len(two))

    # -- forget -------------------------------------------------------------
    store.forget("wiki", "page-a")
    check("forget removes edges in both directions",
          store.neighbors("wiki", "page-a") == []
          and not any(e["kind"] == "wiki"
                      for e in store.neighbors("fact", 1)),
          str(store.neighbors("fact", 1)))

    # -- files + stats (before prune, which removes fake refs) -------------
    files = store.files()
    check("files() lists the real target",
          any(os.path.normcase(str(target)) == os.path.normcase(f["path"])
              for f in files), str(files))
    check("files() reports existence",
          all("exists" in f for f in files), str(files))
    stats = store.stats()
    check("stats counts relations by name",
          isinstance(stats["by_rel"], dict) and stats["by_rel"], str(stats))
    check("export returns renderable edges",
          all("rel" in e and "kind" in e for e in store.export()), "")

    # -- prune -------------------------------------------------------------
    # Uses file-to-file edges on purpose: the other refs in this test (fact 1,
    # wiki page-a) do not exist in the real stores, so prune is right to drop
    # them, and asserting on those would only be testing the fixture.
    missing = os.path.join(tmp, "deleted-file.txt")
    other = pathlib.Path(tmp) / "other.md"
    other.write_text("x", encoding="utf-8")
    store.link("file", str(target), "relates_to", "file", missing)
    store.link("file", str(target), "relates_to", "file", str(other))
    report = store.prune()
    remaining = {os.path.normcase(e["ref_id"])
                 for e in store.neighbors("file", str(target))}
    check("prune removes the edge whose file is gone",
          os.path.normcase(missing) not in remaining, str(report))
    check("prune keeps the edge whose file still exists",
          os.path.normcase(str(other)) in remaining, str(remaining))
    check("prune reports what it checked",
          report["checked"] > 0 and report["removed"] >= 1, str(report))
    check("prune drops relations to refs that no longer exist",
          store.stats()["links"] == 1, str(store.stats()))

    # -- automatic linking --------------------------------------------------
    autolink.link_store = store
    found = autolink.find_paths(f"see {target} for details")
    check("a real path is detected", len(found) == 1, str(found))
    check("the path is not run into the prose after it",
          found and not found[0]["path"].endswith(" for details"), str(found))
    found = autolink.find_paths("edit backend/config.py:42 now")
    check("a relative path with a line number is detected",
          len(found) == 1 and found[0]["line"] == 42, str(found))
    found = autolink.find_paths('open "C:\\My Notes\\todo.md" now')
    check("a quoted path containing spaces is detected",
          len(found) == 1 and found[0]["path"].endswith("todo.md"),
          str(found))
    check("quoted prose is not a file",
          autolink.find_paths('she said "I prefer dark mode"') == [],
          str(autolink.find_paths('she said "I prefer dark mode"')))
    check("a version string is not a file",
          autolink.find_paths('we shipped "v2.0" today') == [],
          str(autolink.find_paths('we shipped "v2.0" today')))
    check("prose is not mistaken for a file",
          autolink.find_paths("use and/or both") == [],
          str(autolink.find_paths("use and/or both")))
    check("URLs are not mistaken for files",
          autolink.find_paths("https://example.com/a/b.txt") == [],
          str(autolink.find_paths("https://example.com/a/b.txt")))
    check("extension-less words are ignored",
          autolink.find_paths("the /usr/local thing") == [],
          str(autolink.find_paths("the /usr/local thing")))

    made = autolink.link_text("fact", 7, f"I read {target} yesterday")
    check("link_text writes a mention", made == 1, str(made))
    check("the mention lands on the file",
          store.neighbors("fact", 7)[0]["kind"] == "file",
          str(store.neighbors("fact", 7)))

    check("provenance links are created",
          autolink.link_provenance("triple", 1, "journal", "2026-09-18")
          is True)
    check("provenance is queryable",
          any(e["rel"] == "derived_from"
              for e in store.neighbors("triple", 1)),
          str(store.neighbors("triple", 1)))
    check("duplicate links are created",
          autolink.link_duplicate("fact", 1, "fact", 2, 0.97)
          and store.neighbors("fact", 1)[0]["rel"] in ("same_as", "mentions",
                                                       "documents"),
          str(store.neighbors("fact", 1)))

    # -- the auto-link switch is honoured ----------------------------------
    from backend.config import config
    original = config._data.get("links", {}).get("auto_file_links")
    config._data.setdefault("links", {})["auto_file_links"] = False
    try:
        check("auto file linking can be switched off",
              autolink.link_text("fact", 8, f"see {target}") == 0)
    finally:
        config._data["links"]["auto_file_links"] = original


def run_hook_tests():
    """facts.add_fact must link, and delete_fact must clean up."""
    import pathlib
    from backend.memory import autolink, facts
    from backend.memory.links import LinkStore

    facts_path = pathlib.Path(facts.__file__).parent / "facts.json"
    backup = facts_path.read_text(encoding="utf-8") if facts_path.exists() else None
    real_links = autolink.link_store
    tmp_store = LinkStore(pathlib.Path(tempfile.mkdtemp()) / "links.db")
    autolink.link_store = tmp_store
    try:
        target = pathlib.Path(tempfile.gettempdir()) / "addled_hook_probe.md"
        target.write_text("probe", encoding="utf-8")
        fact = facts.add_fact(f"I keep notes in {target}", source="test")
        check("add_fact still returns the fact", bool(fact), str(fact))
        if fact:
            check("add_fact links the fact to the file it names",
                  any(e["kind"] == "file"
                      for e in tmp_store.neighbors("fact", fact["id"])),
                  str(tmp_store.neighbors("fact", fact["id"])))
            facts.delete_fact(fact["id"])
            check("delete_fact removes the relations",
                  tmp_store.neighbors("fact", fact["id"]) == [],
                  str(tmp_store.neighbors("fact", fact["id"])))
    finally:
        autolink.link_store = real_links
        if backup is None:
            facts_path.unlink(missing_ok=True)
        else:
            facts_path.write_text(backup, encoding="utf-8")


def main():
    tmp = tempfile.mkdtemp()
    try:
        run_store_tests(tmp)
        run_hook_tests()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


sys.exit(main())
