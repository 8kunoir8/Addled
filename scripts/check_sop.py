"""Standard operating procedure checks.

The two things that decide whether this feature is useful or annoying are
tested directly: that a procedure is only ever offered when it really is close
to the task (the ranking), and that repeated runs sharpen one procedure instead
of cloning near-duplicates (the merge).

The store is redirected to a temp directory for every case, so nothing here
touches the user's real procedure file.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_sop.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def run():
    from backend.config import config
    from backend.sop import learn, match, seeds, store

    config._ensure_loaded()
    original_sop = dict(config._data.get("sop") or {})

    tmp = Path(tempfile.mkdtemp())
    # Only the storage location is overridden: the thresholds under test are the
    # shipped defaults, so this suite would catch a bad default.
    config._data["sop"] = {**original_sop, "dir": str(tmp),
                           "enabled": True, "learn": True}
    try:
        # ---- 1. the store round-trips ------------------------------------
        check("starts empty", store.list_all() == [], str(store.list_all()))
        out = store.upsert({"category": "files", "title": "Read then write",
                            "steps": ["Read the file", "Write the change"],
                            "tools": ["read_file", "write_file"]})
        check("upsert succeeds", out.get("success") is True, str(out))
        sop_id = out["sop"]["id"]
        check("it is stored", len(store.list_all()) == 1, "")
        check("it can be fetched by id", (store.get(sop_id) or {}).get("id") == sop_id, "")
        check("the file is valid JSON",
              isinstance(json.loads(store.path().read_text(encoding="utf-8")), dict),
              "the on-disk file is not readable JSON")

        # Empty procedures teach nothing.
        bad = store.upsert({"category": "files", "title": "Empty one", "steps": []})
        check("an empty procedure is refused", bad.get("success") is False, str(bad))

        # Same category + title updates rather than duplicating.
        store.upsert({"category": "files", "title": "Read then write",
                      "steps": ["Read the file", "Write the change", "Verify"]})
        check("the same title updates in place",
              len(store.list_all()) == 1, f"{len(store.list_all())} entries")

        # ---- 2. record_use -------------------------------------------------
        store.record_use(sop_id, True)
        store.record_use(sop_id, False)
        used = store.get(sop_id)
        check("uses counts both outcomes", used["uses"] == 2, str(used["uses"]))
        check("successes counts only the good one", used["successes"] == 1,
              str(used["successes"]))

        # ---- 3. categories -------------------------------------------------
        store.upsert({"category": "web", "title": "Search then read",
                      "steps": ["Search", "Fetch"]})
        cats = {c["category"]: c["count"] for c in store.categories()}
        check("categories are summarised", cats.get("files") == 1 and cats.get("web") == 1,
              str(cats))
        check("filtering by category works",
              len(store.list_all("files")) == 1, str(store.list_all("files")))
        check("a messy category name is normalised",
              store.upsert({"category": "My Files!!", "title": "t",
                            "steps": ["x"]})["sop"]["category"] == "my_files",
              "category was not normalised")

        # ---- 4. delete -----------------------------------------------------
        check("delete reports success", store.delete(sop_id) is True, "")
        check("it is gone", store.get(sop_id) is None, "")
        check("deleting a missing id is False", store.delete("nope") is False, "")

        # ---- 5. every seed names real tools ------------------------------
        # Guessed tool names would sit in the prompt forever and match nothing.
        try:
            from backend.skills.registry import skill_registry
            known = {s.name for s in skill_registry.list_all()}
            check("skills are registered at all", len(known) > 20, str(len(known)))
            unknown = {}
            for seed in seeds.SEEDS:
                missing = [t for t in seed.get("tools", []) if t not in known]
                if missing:
                    unknown[seed["title"]] = missing
            check("seed procedures only reference tools that exist",
                  not unknown, str(unknown))
        except Exception as e:
            fails.append(f"could not list skills to verify seed tool names: {e}")

        # ---- 6. seeding ----------------------------------------------------
        for sop in store.list_all():
            store.delete(sop["id"])
        added = seeds.seed()
        check("seeding adds the built-ins", added == len(seeds.SEEDS),
              f"added {added} of {len(seeds.SEEDS)}")
        check("seeding twice adds nothing", seeds.seed() == 0, "")
        check("ensure_seeded is a no-op when anything is stored",
              seeds.ensure_seeded() is False, "")
        check("seeded entries are marked as seeds",
              all(s["source"] == "seed" for s in store.list_all()), "")

        # ---- 7. category inference ----------------------------------------
        check("a file task lands in the files category",
              match.guess_category("read the notes file and append a line") == "files",
              match.guess_category("read the notes file and append a line"))
        check("a code task lands in code",
              match.guess_category("fix the bug in this python function") == "code",
              match.guess_category("fix the bug in this python function"))
        check("nonsense falls back to general",
              match.guess_category("zzz qqq") == "general",
              match.guess_category("zzz qqq"))
        check("tools decide the category",
              match.category_for(["read_file", "write_file"]) == "files",
              match.category_for(["read_file", "write_file"]))
        check("no tools means general", match.category_for([]) == "general", "")

        # ---- 8. ranking: the right procedure wins, and only when close ----
        for sop in store.list_all():
            store.delete(sop["id"])
        store.upsert({"category": "files", "title": "Inspect before overwriting",
                      "steps": ["List the folder", "Read the existing file",
                                "Write the change", "Read it back"],
                      "tools": ["list_dir", "read_file", "write_file"]})
        store.upsert({"category": "web", "title": "Search then read the source",
                      "steps": ["Search the web", "Fetch the best result"],
                      "tools": ["web_search", "fetch_webpage"]})

        hits = match.find("read the existing file before I overwrite it",
                          category="files", limit=3)
        check("a paraphrase finds the file procedure",
              hits and hits[0]["sop"]["title"] == "Inspect before overwriting",
              str([h["sop"]["title"] for h in hits]))

        hit = match.best("read the existing file before I overwrite it")
        check("best() returns it above the threshold", hit is not None, "nothing scored high enough")
        check("best() does not offer the web procedure",
              hit is None or "file" in hit["sop"]["category"], str(hit and hit["sop"]["title"]))

        miss = match.best("what is the capital of Peru")
        check("an unrelated task gets no procedure", miss is None,
              f"offered {miss and miss['sop']['title']}")

        block = match.build_sop_context("read the existing file before I overwrite it")
        check("a fitting task produces a [Procedure] block",
              block and block.startswith("[Procedure]"), str(block))
        check("the block lists the steps in order",
              block and "1. List the folder" in block and "4. Read it back" in block,
              str(block))
        check("an unrelated task produces no block at all",
              match.build_sop_context("what is the capital of Peru") is None,
              "an empty or wrong block was produced")

        # ---- 9. learning: create once, then merge -------------------------
        for sop in store.list_all():
            store.delete(sop["id"])

        skipped = learn.record_run("files", ["read_file"], "read a file")
        check("a single-tool run is not a procedure",
              "skipped" in skipped, str(skipped))
        skipped = learn.record_run("files", ["read_file", "write_file"],
                                   "read then write", success=False)
        check("a failed run is not learned", "skipped" in skipped, str(skipped))

        first = learn.record_run("files", ["read_file", "write_file"],
                                 "read config then write the new value", success=True)
        check("a two-tool successful run is learned",
              "created" in first, str(first))
        check("exactly one procedure exists now",
              len(store.list_all("files")) == 1, str(len(store.list_all("files"))))

        second = learn.record_run("files", ["read_file", "write_file"],
                                  "read config then write the new value", success=True)
        check("the same run again merges instead of duplicating",
              "merged" in second, str(second))
        check("and still leaves one procedure",
              len(store.list_all("files")) == 1, str(len(store.list_all("files"))))
        only = store.list_all("files")[0]
        check("the merge counted the extra use", only["uses"] >= 2, str(only["uses"]))

        third = learn.record_run("files", ["read_file", "write_file", "list_dir"],
                                 "read config then write the new value", success=True)
        check("a wider tool set merges too", "merged" in third, str(third))
        merged_tools = store.list_all("files")[0]["tools"]
        check("and the new tool is absorbed",
              "list_dir" in merged_tools, str(merged_tools))

        # ---- 10. learning never raises ------------------------------------
        check("record_run survives garbage",
              isinstance(learn.record_run(None, None, None, success=True), dict), "")
        check("record_run survives a bad category",
              isinstance(learn.record_run(12345, ["a", "b"], "x"), dict), "")

        # ---- 11. off switches ---------------------------------------------
        config._data["sop"]["learn"] = False
        off = learn.record_run("web", ["web_search", "fetch_webpage"], "search then read")
        check("learning off is a no-op", "skipped" in off, str(off))
        config._data["sop"]["learn"] = True

        config._data["sop"]["enabled"] = False
        check("procedures off means no prompt block",
              match.build_sop_context("read the existing file before overwriting") is None,
              "a block was built while the feature is off")
        check("procedures off blocks learning",
              "skipped" in learn.record_run("web", ["web_search", "fetch_webpage"], "x"),
              "")
        check("describe() reports the switch", store.describe()["enabled"] is False, "")
        config._data["sop"]["enabled"] = True

        # ---- 12. a corrupt file is survivable -----------------------------
        store.path().write_text("{not json", encoding="utf-8")
        check("a corrupt file reads as empty", store.list_all() == [],
              "a corrupt file was not handled")
        out = store.upsert({"category": "files", "title": "After corruption",
                            "steps": ["Do the thing"]})
        check("and can be written over", out.get("success") is True, str(out))

        # ---- 13. interactions don't leak into each other -------------------
        other = match.find("")
        check("an empty task matches nothing", other == [], str(other))
        check("describe() is safe to call", isinstance(store.describe(), dict), "")
    finally:
        config._data["sop"] = original_sop
        shutil.rmtree(tmp, ignore_errors=True)


run()
print()
print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
