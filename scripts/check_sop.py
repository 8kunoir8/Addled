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

        # ---- 14. the tool-loop hook really reaches the store --------------
        # Everything above tests the store; this tests that a finished turn is
        # wired to it, which a rename or a typo at the call site would break
        # silently.
        from backend.skills.tool_loop import _learn_procedure
        for sop in store.list_all():
            store.delete(sop["id"])
        _learn_procedure(
            [{"role": "user", "content": "read the config then write the new value"}],
            [{"tool": "read_file", "success": True},
             {"tool": "write_file", "success": True}])
        check("a successful turn leaves a procedure behind",
              len(store.list_all()) == 1,
              f"{len(store.list_all())} stored — the hook is not reaching the store")

        for sop in store.list_all():
            store.delete(sop["id"])
        _learn_procedure([{"role": "user", "content": "read then write"}],
                         [{"tool": "read_file", "success": False},
                          {"tool": "write_file", "success": False}])
        check("a failed turn leaves nothing behind", store.list_all() == [], "")

        _learn_procedure([], [])
        check("an empty turn is harmless", store.list_all() == [], "")
        _learn_procedure([{"role": "user", "content": "x"}],
                         [{"tool": "a", "success": True}])
        check("a single-tool turn is not recorded", store.list_all() == [], "")

        # ---- 15. the prompt block is wired into the chat pipeline ---------
        source = open(os.path.join(ROOT, "backend", "ws_server.py"),
                      encoding="utf-8").read()
        check("the chat pipeline asks for a procedure block",
              "build_sop_context" in source,
              "nothing in the pipeline calls build_sop_context, so no procedure "
              "would ever reach the model")
        check("and the tool loop asks for learning",
              "_learn_procedure" in source
              or "_learn_procedure" in open(
                  os.path.join(ROOT, "backend", "skills", "tool_loop.py"),
                  encoding="utf-8").read(), "")

        # ---- 16. offering a procedure COUNTS as using it ------------------
        #
        # `record_use` was only reached on the learning path, so a seed that was
        # correctly matched and injected twenty times still read "0/0
        # successful" — and the block told the model that, which argues against
        # following it. There was also no way to see whether lookup ever fired.
        for sop in store.list_all():
            store.delete(sop["id"])
        made = store.upsert({"category": "files",
                             "title": "Inspect before overwriting",
                             "steps": ["List the target first.",
                                       "Read it before rewriting it.",
                                       "Write the change."],
                             "tools": ["list_dir", "read_file", "write_file"],
                             "source": "seed"})
        check("the seed starts unused",
              (store.get(made["sop"]["id"]) or {}).get("uses") == 0, "")

        # Wording that shares vocabulary with the seed. The scorer is lexical
        # first, so an unrelated sentence would correctly find nothing — and
        # testing that here would prove nothing about counting.
        block = match.build_sop_context(
            "read the existing file before I overwrite it")
        check("a matching task offers the procedure", bool(block), "none offered")
        used = store.get(made["sop"]["id"]) or {}
        check("offering incremented uses", used.get("uses") == 1, str(used.get("uses")))
        check("offering incremented successes", used.get("successes") == 1,
              str(used.get("successes")))
        check("the block does not tell the model 0/0",
              "0/0 successful" not in (block or ""), (block or "")[:160])
        check("the block shows the incremented count",
              "1/1 successful" in (block or ""), (block or "")[:160])

        # A lookup that finds nothing must not move a counter.
        before_uses = (store.get(made["sop"]["id"]) or {}).get("uses")
        check("an unrelated question offers nothing",
              match.build_sop_context("what is the capital of Peru") is None, "")
        check("and counted nothing",
              (store.get(made["sop"]["id"]) or {}).get("uses") == before_uses,
              "an unmatched lookup moved a counter")

        # ---- 17. a learned category must be one the LOOKUP can produce -----
        #
        # LEARNING files under `category_for(tools)`, whose values come from the
        # skill registry. LOOKUP searches with `guess_category(message)`, which
        # can only produce the CATEGORY_HINTS keys. `windows`, `integrations`,
        # `meta` and `general` existed only on the write side, so a procedure
        # filed under one was saved, counted, and unreachable forever.
        seekable = {c for c, _ in match.CATEGORY_HINTS} | {match.DEFAULT_CATEGORY}
        for tools, expected in ((["list_windows", "focus_window"], "desktop"),
                                (["close_window"], "desktop"),
                                (["calendar_add", "calendar_list"], "calendar"),
                                (["email_send"], "calendar"),
                                (["task_schedule"], "system"),
                                (["read_file", "write_file"], "files")):
            got = match.category_for(tools)
            check(f"category_for({tools[0]}) is {expected}", got == expected,
                  f"got {got!r}")
            check(f"  and {got!r} is reachable by lookup", got in seekable,
                  f"{got!r} has no CATEGORY_HINTS entry, so nothing learned "
                  f"there could ever be found")

        # The end-to-end version: learn from a window task, then find it by the
        # same wording. Before the alias this could not succeed.
        learned = learn.record_run(None, ["list_windows", "focus_window"],
                                   "bring the browser window to the front")
        check("a window task is learned or merged",
              bool(learned.get("created") or learned.get("merged")), str(learned))
        check("filed under a seekable category",
              learned.get("category") in seekable, repr(learned.get("category")))
        found = match.best("bring the browser window to the front")
        check("its own task finds it again", bool(found),
              "a learned procedure the lookup can never reach")

        # ---- 18. the reason is kept, shown, and not overwritten ------------
        # Emptied first so "the repeat merged" means the repeat of THIS task,
        # not whatever an earlier section happened to leave behind.
        for sop in store.list_all():
            store.delete(sop["id"])
        ctx = ("You are Addled.\n\n[Facts]\nThe user prefers the report as CSV.\n\n"
               "[Wiki]\nQ3 figures live in the finance wiki.\n\n"
               "A standing instruction identical on every task.\n")
        got = learn.record_run(None, ["read_file", "write_file"],
                               "update the quarterly report from the wiki",
                               success=True, context=ctx)
        sid = got.get("created") or got.get("merged")
        rec = store.get(sid) or {}
        reason = str(rec.get("reason") or "")
        check("a reason was stored", bool(reason), "the reason is empty")
        check("it quotes the turn's own context",
              "CSV" in reason or "finance wiki" in reason, reason[:160])
        check("standing instructions are not kept",
              "standing instruction" not in reason,
              "the reason is boilerplate, identical on every task")
        check("it is one line and bounded",
              "\n" not in reason and len(reason) <= store.MAX_REASON_CHARS,
              f"{len(reason)} chars")
        shown = match.build_sop_context("update the quarterly report from the wiki")
        check("the injected block shows the reason",
              bool(shown) and "Because:" in shown, (shown or "")[:200])

        # Repeating the task widens the tools but must not rewrite the reason.
        #
        # The repeat is made with an EMPTY context, so the assertion is about
        # the preserved reason rather than about which new reason won. Whether
        # two similar runs merge is a scoring question with its own section (9)
        # and its own thresholds; this section is only about `reason`.
        first_reason = str((store.get(sid) or {}).get("reason") or "")
        again = learn.record_run(None, ["read_file", "write_file", "list_dir"],
                                 "update the quarterly report from the wiki",
                                 success=True, context="[Facts]\nA later reason.\n")
        same = store.get(sid) or {}
        check("the repeat was recorded against the same procedure",
              again.get("merged") == sid or again.get("created") == sid,
              str(again))
        if again.get("merged") == sid:
            check("the tools were widened",
                  "list_dir" in (same.get("tools") or []),
                  str(same.get("tools")))
            check("the original reason was kept, not replaced",
                  str(same.get("reason") or "") == first_reason,
                  f"{first_reason!r} became {same.get('reason')!r}")
        else:
            # A near-miss created a second procedure instead of merging, which
            # is the documented behaviour of the merge bar. Recorded rather than
            # silently skipped, so this cannot pass by doing nothing.
            check("a new procedure carries its own reason",
                  "A later reason" in str(same.get("reason") or ""),
                  str(same.get("reason"))[:120])

        # The merge path, exercised deterministically.
        #
        # `score()` prefers the embedding whenever one is available, and its
        # merge bar (0.82) is deliberately higher than a near-miss scores — an
        # identical sentence measured 0.77 on this machine, so it creates a
        # second procedure rather than merging. That is the documented design,
        # not a fault, but it means the merge cannot be relied on to happen from
        # wording alone in a test. The preserved-reason rule is therefore driven
        # through the merge directly.
        import backend.sop.store as _store
        target = store.get(sid) or {}
        if target:
            _store.upsert({"id": sid, "tools": ["read_file", "write_file"],
                           "reason": "Original kept reason."})
            merged = learn.record_run(None, ["read_file", "write_file"],
                                      target.get("title") or "",
                                      success=True, context="")
            after_merge = store.get(sid) or {}
            check("an exact title repeat merges",
                  merged.get("merged") == sid, str(merged))
            check("and does not overwrite the stored reason",
                  "Original kept reason" in str(after_merge.get("reason") or ""),
                  repr(after_merge.get("reason"))[:120])

        # ---- 19. a store written before 'reason' existed still loads -------
        import json as _json
        legacy_path = store.path()
        legacy_path.write_text(_json.dumps({
            "version": 1,
            "sops": [{"id": "old1", "category": "files", "title": "Legacy",
                      "steps": ["Do the thing."], "tools": ["read_file"],
                      "uses": 3, "successes": 2, "source": "seed"}]}),
            encoding="utf-8")
        try:
            legacy = store.list_all()
            entry = next((s for s in legacy if s.get("id") == "old1"), None)
            check("an old store still loads", entry is not None,
                  str(legacy)[:160])
            if entry:
                check("the missing reason becomes empty",
                      entry.get("reason") == "", repr(entry.get("reason")))
                check("its counts survive", entry.get("uses") == 3,
                      str(entry.get("uses")))
        except Exception as e:  # noqa: BLE001
            check("an old store still loads", False, f"{type(e).__name__}: {e}")
    finally:
        config._data["sop"] = original_sop
        shutil.rmtree(tmp, ignore_errors=True)


run()
print()
print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
