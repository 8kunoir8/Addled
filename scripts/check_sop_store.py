"""The procedure store's new behaviour: guards, top-up, and deletion memory.

Three things were added and none of them had a test:

  * `guards` — preconditions attached to a procedure
  * top-up — a new built-in reaches an install that already has procedures
  * the tombstone — a deleted seed does NOT come back

Run from the project root:

    .\\python-bundle\\python.exe -s .\\scripts\\check_sop_store.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "E:/Kunoir/Codeground/Clicky/Addled")

from backend.sop import seeds, store  # noqa: E402

fails: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        fails.append(label)
        print(f"  FAIL  {label}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    box = Path(tempfile.mkdtemp(prefix="sopstore_"))
    original = store.base_dir
    try:
        # Point the store at a throwaway directory. `base_dir` is what `path()`
        # is built from, so nothing touches the real procedure file.
        store.base_dir = lambda: box  # type: ignore[assignment]

        print("A. guards round-trip")
        out = store.upsert({
            "category": "files",
            "title": "Test procedure",
            "steps": ["one", "two"],
            "tools": ["read_file"],
            "guards": ["the target exists", "the output is new"],
        })
        check("upsert accepts guards", out.get("success") is True)
        saved = store.find_by_title("files", "Test procedure") or {}
        check("guards are stored", saved.get("guards") ==
              ["the target exists", "the output is new"], str(saved.get("guards")))

        reloaded = store.load()["sops"]
        rec = next((s for s in reloaded if s["title"] == "Test procedure"), {})
        check("guards survive a reload", rec.get("guards") ==
              ["the target exists", "the output is new"], str(rec.get("guards")))

        print("\nB. an OLD store without guards still loads")
        raw = json.loads(store.path().read_text(encoding="utf-8"))
        for s in raw["sops"]:
            s.pop("guards", None)
        raw.pop("removed_seeds", None)
        store.path().write_text(json.dumps(raw), encoding="utf-8")
        loaded = store.load()
        check("a store with no guards key loads", len(loaded["sops"]) > 0)
        check("missing guards read as empty, not missing",
              all("guards" in s for s in loaded["sops"]))
        check("a missing removed_seeds reads as empty",
              loaded.get("removed_seeds") == [])

        print("\nC. top-up reaches an install that already has procedures")
        # Empty the store, seed ONE entry the way an old install would have it.
        store.path().unlink(missing_ok=True)
        store.upsert({"category": "files", "title": "Pre-existing",
                      "steps": ["x"], "tools": ["read_file"],
                      "source": "seed"})
        before = len(store.load()["sops"])
        added = seeds.ensure_seeded()
        after = len(store.load()["sops"])
        check("ensure_seeded adds on a non-empty store", added > 0,
              f"added={added}")
        check("and the count went up", after > before, f"{before} -> {after}")
        titles = {s["title"] for s in store.load()["sops"]}
        check("the pre-existing procedure is untouched", "Pre-existing" in titles)
        # Named from the seed list, not hard-coded: a title gets merged or
        # reworded and the check should follow, not fail.
        want = seeds.SEEDS[0]["title"]
        check("a new built-in arrived", want in titles,
              f"looked for {want!r} in {sorted(titles)}")

        print("\nD. top-up is idempotent")
        again = seeds.ensure_seeded()
        check("calling it twice adds nothing the second time", again == 0,
              f"added={again}")

        print("\nE. a deleted seed does NOT come back")
        # The first seed by name rather than a hard-coded title: seeds get
        # merged and renamed (the two file procedures became one), and a test
        # that names one breaks for the wrong reason when that happens.
        seeded = seeds.SEEDS[0]
        victim = store.find_by_title(seeded["category"], seeded["title"])
        check("the seed is there to delete", victim is not None,
              f"looked for {seeded['title']!r}")
        if victim is None:
            return 1
        store.delete(victim["id"])
        check("it is gone after delete",
              store.find_by_title(seeded["category"], seeded["title"]) is None)
        seeds.ensure_seeded()
        check("and a top-up does not resurrect it",
              store.find_by_title(seeded["category"], seeded["title"]) is None,
              "the tombstone did not hold")
        check("other seeds were still topped up",
              store.find_by_title("code", "Change code safely") is not None)

        print("\nF. the tombstone survives a reload")
        keys = store.removed_seed_keys()
        check("the deletion is recorded", len(keys) >= 1, str(keys))
        check("the record names the category and title",
              any(seeded["category"] in k for k in keys)
              and any(seeded["title"].lower()[:20] in k for k in keys),
              str(keys))
    finally:
        store.base_dir = original  # type: ignore[assignment]
        import shutil
        shutil.rmtree(box, ignore_errors=True)

    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s): " + "; ".join(fails))
        return 1
    print("PASS: guards, top-up and the deletion tombstone all behave")
    return 0


if __name__ == "__main__":
    sys.exit(main())
