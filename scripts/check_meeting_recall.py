"""Meeting recall: a summarised meeting becomes answerable, and a deleted one
stops being answerable.

Phase 6 of the meeting-notes plan. The property under test is not "indexing
runs" but the two halves that make it useful and the one that makes it safe:

  * A summarised meeting goes into semantic recall under its OWN category, so
    "what did we decide about X?" can reach it without every chat turn having
    to be searched.
  * Deleting a meeting removes exactly its row — not the whole category. The
    `session_summary` module deleted a category once and silently erased every
    other summary's row, so this asserts the neighbours SURVIVE.
  * Re-summarising replaces a meeting's row rather than leaving two versions
    of the same meeting in the index to compete.

The vector store falls back to a hash embedder when no semantic model loads,
and this box HAS one (`transformers 5.14.1` + `torch` are installed;
`embedder_id()` is `transformers:minilm:v1`). Run this suite WITHOUT `-s` to
measure that: `-s` suppresses `site-packages`, so `transformers` fails to
import and the suite silently degrades to the hash path — which is how a
"hash embedder" conclusion reached the plan doc and later had to be retracted.
Reachability is asserted either way; the checks below report which of the two
paths they actually ran rather than skipping in silence.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_meeting_recall.py
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Isolated before anything imports a store, so the real meetings directory and
# the real vector index are never touched.
_TMP = tempfile.mkdtemp(prefix="addled_meeting_recall_check_")
os.environ["ADDLED_DATA_DIR"] = _TMP

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f" — {detail}" if not cond and detail else ""))


from backend.meetings import store  # noqa: E402
from backend.memory.recall import embed_text  # noqa: E402
from backend.memory.vector_store import vector_store  # noqa: E402

if not vector_store.available:
    print("vector store unavailable in this environment — cannot check recall")
    sys.exit(0)


def _row_count(category):
    return len(vector_store.list(category=category, limit=1000))


print("Indexing a summarised meeting")

_m = store.create("Launch readiness sync", source="file")
store.set_summary(_m["id"], "We agreed to ship the launch on the 22nd.",
                  decisions=["Ship the launch on the 22nd"],
                  actions=[{"what": "Send the migration plan", "who": "Tom"}])

check("a meeting with no summary is not indexed",
      store.index(store.create("Empty meeting", source="file")["id"]) is None,
      "an unsummarised meeting produced a vector row")

_row = store.index(_m["id"])
check("indexing a summarised meeting returns a row id",
      isinstance(_row, int), repr(_row))

_m = store.get(_m["id"])
check("the row id is recorded ON the meeting",
      _m.get("vector_id") == _row, repr(_m.get("vector_id")))

check("the meeting is in its own category",
      _row_count("meeting") == 1, f"{_row_count('meeting')} meeting rows")

_other = store.create("Budget review", source="file")
store.set_summary(_other["id"], "We cut the Q3 budget by ten percent.")
store.index(_other["id"])
check("a second meeting is indexed too", _row_count("meeting") == 2,
      f"{_row_count('meeting')} meeting rows")


print("\nReachability — the point of the exercise")


def _find(query, category):
    return vector_store.search(embed_text(query), category=category,
                               top_k=10, min_similarity=-1.0)


_hits = _find("What did we decide about the launch date?", "meeting")
check("a question about the meeting reaches a 'meeting' row", len(_hits) >= 1,
      f"{len(_hits)} hits")
check("and the row carries the meeting id",
      any((h.get("metadata") or {}).get("meeting_id") == _m["id"]
          for h in _hits),
      str([(h.get("metadata") or {}).get("meeting_id") for h in _hits]))

# The old path must NOT see it. If it does, the summaries and the meetings are
# sharing a category and the two would compete for the same slots.
_conv = _find("What did we decide about the launch date?", "conversation")
_mixed = [h for h in _conv
          if (h.get("metadata") or {}).get("meeting_id") == _m["id"]]
check("conversation recall does not see the meeting",
      not _mixed, "the meeting leaked into the conversation category")


print("\nRe-summarising replaces, it does not accumulate")

store.set_summary(_m["id"], "We moved the launch to the 29th.",
                  decisions=["Move the launch to the 29th"])
_second = store.index(_m["id"])
check("re-indexing returns a new row id", _second != _row,
      f"old={_row} new={_second}")

_all = vector_store.list(category="meeting", limit=1000)
_ids = [(r.get("metadata") or {}).get("meeting_id") for r in _all]
check("the meeting appears exactly once after re-summarising",
      _ids.count(_m["id"]) == 1, f"{_ids.count(_m['id'])} rows for {_m['id']}")
check("the other meeting was left alone", _ids.count(_other["id"]) == 1,
      f"{_ids.count(_other['id'])} rows for {_other['id']}")
check("the stale row is gone",
      vector_store.get(_row) is None if hasattr(vector_store, "get") else True,
      "the pre-re-summarise row is still present")


print("\nDeleting removes one row, not the category")


class _Box:
    """Capture how many rows each category had before the delete, so the
    assertion is about the SIBLINGS surviving rather than a hard-coded 1."""

    before = None


_BOX = _Box()
_BOX.before = _row_count("meeting")
check("delete reports success", store.delete(_m["id"]) is True)
check("the meeting is really gone from the store",
      store.get(_m["id"]) is None)

_after = _row_count("meeting")
check("only that meeting's row was removed",
      _after == _BOX.before - 1 and _after == 1,
      f"before={_BOX.before} after={_after}")

_survivors = [(r.get("metadata") or {}).get("meeting_id")
              for r in vector_store.list(category="meeting", limit=1000)]
check("the OTHER meeting's row survived the delete",
      _other["id"] in _survivors,
      "deleting one meeting removed another (the delete_category bug)")

# And the deleted meeting is no longer answerable.
_hits2 = [h for h in _find("What did we decide about the launch date?",
                           "meeting")
          if (h.get("metadata") or {}).get("meeting_id") == _m["id"]]
check("the deleted meeting is no longer reachable by recall",
      not _hits2, "a deleted meeting still answered a question")


print("\nThe recall block itself")


import asyncio  # noqa: E402
from backend.memory import relevance  # noqa: E402


async def _block(q):
    return await relevance.meetings_block(q)


_other_meeting = store.get(_other["id"])
_block_text = asyncio.run(_block("How did the budget review go?"))
check("meetings_block returns something when a meeting is relevant",
      bool(_block_text), repr(_block_text)[:120])
check("the block names the meeting", "Budget review" in (_block_text or ""),
      "the title is missing, so the model cannot say where this came from")
check("the block says whose words these are",
      "other people" in (_block_text or "").lower(),
      "the provenance line is missing")

_block_none = asyncio.run(relevance.meetings_block("zzz qqq unrelated xyzzy"))
check("an unrelated query yields no meeting block (or only relevant ones)",
      _block_none is None or "Budget review" not in _block_none,
      repr(_block_none)[:120])

_empty = store.compose_meeting_block([])
check("composing no meetings yields None, not an empty header", _empty is None,
      repr(_empty))

_no_summary = store.compose_meeting_block([{"id": "x", "title": "T",
                                            "summary": ""}])
check("a meeting with no summary is skipped", _no_summary is None,
      repr(_no_summary))


print("\nWiring — the block is actually injected")


_ws = open(os.path.join(ROOT, "backend", "ws_server.py"),
           encoding="utf-8").read()
check("ws_server asks for a meeting block",
      "relevance.meetings_block" in _ws,
      "the block exists but nothing injects it")

_reg = open(os.path.join(ROOT, "backend", "skills", "registry.py"),
            encoding="utf-8").read()
check("the summarise skill indexes after summarising",
      "store.index(meeting_id)" in _reg,
      "a summary made through the skill would never be answerable")

_store_src = open(os.path.join(ROOT, "backend", "meetings", "store.py"),
                  encoding="utf-8").read()
check("delete unindexes through the store",
      "unindex(meeting_id)" in _store_src,
      "deleting a meeting would leave it answerable")


print("\nThe index is READ, not just written")

# The gap that let write-only code ship for a whole phase: every check above
# proves a row EXISTS and is searchable *with an explicit category*. Nothing
# proved the app ever searched that category, and it did not -- both recall
# paths hardcoded `category="conversation"`, so meetings were reachable only
# through the file listing. A row nothing reads is not recall.
_rel = open(os.path.join(ROOT, "backend", "memory", "relevance.py"),
            encoding="utf-8").read()

# These assert BEHAVIOUR where they can. Grepping the source for
# `category="meeting"` was the first attempt and it was worthless: the string
# also appears in a comment in the same file, so a revert that made the index
# write-only again still passed on its own prose. A check satisfied by text
# that is not the code is not a check.
import asyncio  # noqa: E402
from backend.memory import relevance as _relevance  # noqa: E402

_index_calls: list = []
_listing_calls: list = []


async def _spy_index(query, want):
    _index_calls.append(query)
    return []


async def _spy_listing(want):
    _listing_calls.append(want)
    return []


_real_index = _relevance._meetings_by_index
_real_listing = _relevance._meetings_by_listing
_relevance._meetings_by_index = _spy_index
_relevance._meetings_by_listing = _spy_listing
try:
    asyncio.run(_relevance.meetings_block("does the index get consulted?"))
finally:
    _relevance._meetings_by_index = _real_index
    _relevance._meetings_by_listing = _real_listing

check("meetings_block consults the semantic index",
      len(_index_calls) == 1,
      "the index was never queried -- store.index() rows are write-only")
check("the index is queried with the user's question",
      bool(_index_calls) and _index_calls[0] == "does the index get consulted?",
      f"queried with {_index_calls!r}")
check("the listing fallback also runs",
      len(_listing_calls) == 1,
      "no fallback: a hash-embedder machine would lose meeting recall")

# The search must use the MEETING category, asserted by capturing the argument
# rather than reading the file.
import backend.memory.vector_store as _vs  # noqa: E402
from backend.memory.embedding import embedder_kind  # noqa: E402

_cats: list = []
_real_search = _vs.vector_store.search


def _spy_search(query, category=None, **kw):
    _cats.append(category)
    return []


_vs.vector_store.search = _spy_search
try:
    asyncio.run(_relevance._meetings_by_index("renewal", 3))
finally:
    _vs.vector_store.search = _real_search

if embedder_kind() == "hash":
    # The hash embedder short-circuits before searching, and that is correct:
    # comparing meaningless vectors would return noise. Say which case ran
    # rather than skipping silently, so this does not read as coverage.
    print("  ..   hash embedder loaded, so the index short-circuits and "
          "the listing fallback is the live path here")
    check("a hash embedder does not attempt a meaningless search",
          not _cats, f"searched anyway with categories {_cats!r}")
else:
    check("the index search uses the meeting category",
          bool(_cats) and all(c == "meeting" for c in _cats),
          f"searched categories {_cats!r}")

check("the index helper is actually awaited",
      "await _meetings_by_index(" in _rel,
      "the index helper is defined but never called")

# And the two passes must not double-list the same meeting.
check("the two passes de-duplicate by meeting id",
      "in have" in _rel and "have.add(mid)" in _rel,
      "a meeting found by both passes would appear twice in the block")


print("\nThe index uses the SAME embedder that recall searches with")

# The bug this catches, measured on the live store: `store.index()` imported
# `embed_text` from `backend.memory.recall`, which defines it as the LEGACY
# HASH embedder. Every meeting row was therefore written as a hash vector while
# `_meetings_by_index` searched with MiniLM. Both are 384-dim, so the store
# accepted the mixture without complaint and returned confident nonsense --
# `reembed.py`'s docstring describes this exact failure mode. Reading the
# source for the right import name would be a check satisfied by a string, so
# this indexes a real meeting and compares the stored vector against a fresh
# embed of its own text.
from backend.memory.embedding import embed_text as _embed, embedder_kind as _kind  # noqa: E402
from backend.memory.vector_store import vector_store as _vs2  # noqa: E402
import json as _json  # noqa: E402
import numpy as _np  # noqa: E402

if _kind() == "hash":
    print("  ..   hash embedder loaded, so there is no second embedder to "
          "disagree with; skipping the comparison")
else:
    _emb_meeting = store.create(title="Embedder consistency probe")
    store.set_summary(_emb_meeting["id"],
                      "We agreed the renewal terms and settled the annual "
                      "pricing ceiling with the vendor.")
    _emb_row = store.index(_emb_meeting["id"])
    _raw = _vs2._conn.execute(
        "SELECT embedding, metadata FROM vectors WHERE id=?",
        (_emb_row,)).fetchone()
    _stored = _np.frombuffer(_raw[0], dtype=_np.float32)
    _meta = _json.loads(_raw[1]) or {}
    _text = str(_meta.get("text") or "")
    _fresh = _np.asarray(_embed(_text), dtype=_np.float32).flatten()
    if _stored.shape[0] != _fresh.shape[0]:
        _cos = -1.0
    else:
        _cos = float(_np.dot(_stored, _fresh) /
                     ((_np.linalg.norm(_stored) * _np.linalg.norm(_fresh)) or 1))
    check("a stored meeting vector matches a fresh embed of its own text",
          _cos > 0.99,
          f"cosine {_cos:.3f} -- the row was written with a DIFFERENT "
          f"embedder than recall searches with, so it is unsearchable")
    store.delete(_emb_meeting["id"])

_store_src = open(os.path.join(ROOT, "backend", "meetings", "store.py"),
                  encoding="utf-8").read()
check("the index does not use recall's legacy hash embed_text",
      "from backend.memory.recall import embed_text" not in _store_src,
      "recall.embed_text is the legacy hash embedder; importing it writes "
      "vectors that cannot be compared with anything recall searches with")


import shutil  # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)

print()
if fails:
    print(f"{len(fails)} FAILED")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All meeting recall checks passed.")
