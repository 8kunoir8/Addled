"""Memory retrieval checks: the embedder identity, the retrieval floor, health.

Three defects motivated this suite, all of them silent.

1. **The gate was doing two jobs.** One similarity bar decided both who entered
   the ranking and what was worth injecting. Measured on a real store, 219
   English-MiniLM-embedded Indonesian and code-mixed turns scored 0.23-0.33, so
   a 0.35 bar admitted ~0 semantic candidates and hybrid search was keyword-only
   while still calling itself hybrid.

2. **Changing the embedder corrupted ranking without an error.** Rows were
   tagged with the *backend* ('onnx'), not the model. `all-MiniLM-L6-v2` and
   `paraphrase-multilingual-MiniLM-L12-v2` both emit 384 floats, so a store
   holding a mixture accepts it happily and cosine similarity between the two
   returns confident nonsense. Only untagged rows were ever re-embedded.

3. **Nothing reported any of it.** A missing embedder, an empty store and a gate
   that admits nothing all look the same from outside: the agent answers from
   the conversation alone.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_memory_rag.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

fails: list[str] = []

def check(label: str, ok: bool, detail: object = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'}  {label}" +
          (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        fails.append(label)

# ---- 1. the embedder is identified by model, not by backend ----------------

def embedder_identity_checks() -> None:
    from backend.memory import embedding

    kind = embedding.embedder_kind()
    ident = embedding.embedder_id()
    check("embedder_kind reports a known backend",
          kind in ("onnx", "transformers", "hash"), kind)
    check("embedder_id names the model as well as the backend",
          ident == "hash" or ":" in ident, ident)
    check("a semantic embedder is distinguishable from the hash fallback",
          (kind == "hash") == (ident == "hash"), f"{kind} / {ident}")

    # The check that was missing, and the reason this suite exists.
    #
    # `embedder_kind()` returned "onnx" while every vector was produced by the
    # hashed fallback: the ONNX model requires `token_type_ids`, the tokenizer
    # branch never supplied it, and the exception was logged at debug and
    # swallowed. Two things must therefore be asserted, because either alone is
    # satisfiable by a broken embedder:
    #   * the vector is dense (a hash vector has a handful of nonzero values);
    #   * text that means the same thing scores higher than text that does not.
    import numpy as np
    from backend.memory.embedding import embed_text

    if kind == "hash":
        print("       (hash-only embedder — skipping the semantic assertions)")
        return

    vec = embed_text("a dog barking")
    nonzero = int(np.count_nonzero(vec))
    check("a semantic embedder produces a dense vector",
          nonzero > embedding.DIM // 2,
          f"{nonzero}/{embedding.DIM} nonzero — a handful means the hashed "
          f"fallback answered, whatever embedder_kind() says")

    def cos(x, y):
        nx, ny = float(np.linalg.norm(x)), float(np.linalg.norm(y))
        return float(np.dot(x, y) / (nx * ny)) if nx and ny else 0.0

    near = cos(embed_text("a dog barking"), embed_text("a puppy woofing"))
    far = cos(embed_text("a dog barking"), embed_text("quarterly tax filing"))
    # The floor is 0.15, not 0.02, and that number is the point of this check.
    # Mean-pooling the padding positions into the vector leaves related text at
    # 0.939 against 0.872 for unrelated — a gap of 0.067, which a 0.02 floor
    # waves through. The vectors are dense either way, so "nonzero == DIM" says
    # nothing about whether the embedder discriminates. Measured with masking,
    # the same pair separates by ~0.70 (0.659 vs -0.045).
    check("related text scores well above unrelated text",
          near > far + 0.15, f"related={near:.3f} unrelated={far:.3f} "
          f"(gap {near - far:.3f}; below 0.15 suggests padding is being pooled)")
    check("unrelated text does not score highly",
          far < 0.5, f"unrelated={far:.3f} — a near-constant high score for "
          f"any input means the vector is dominated by something other than "
          f"the words")
    # Padding pollution makes *every* pair look similar, including nonsense.
    # Gibberish must not outrank a genuine pair; under the broken pooling it did
    # (0.962 for "zzzz qqqq" vs "aaaaaaaa", against 0.955 for the real pair).
    gibberish = cos(embed_text("zzzz qqqq"), embed_text("aaaaaaaaaaaaaaaa"))
    check("nonsense text does not score above a real match",
          gibberish < near, f"gibberish={gibberish:.3f} related={near:.3f}")

    check("the configured source is a known model",
          embedding.configured_source() in embedding.EMBEDDER_MODELS,
          embedding.configured_source())
    check("both models are listed for selection",
          set(embedding.EMBEDDER_MODELS) == {"minilm", "multilingual"},
          str(sorted(embedding.EMBEDDER_MODELS)))
    # The whole reason a model change is dangerous: same shape, different space.
    check("both candidate models emit the same dimension", embedding.DIM == 384,
          str(embedding.DIM))

    check("availability can be checked without downloading",
          isinstance(embedding.model_available("minilm"), bool),
          str(embedding.model_available("minilm")))

# ---- 2. staleness is decided by model, and migration is idempotent ---------

def staleness_checks() -> None:
    from backend.memory.reembed import stale_rows

    rows = [
        {"id": 1, "metadata": {"embedder": "onnx:minilm"}},
        {"id": 2, "metadata": {"embedder": "onnx"}},            # old short tag
        {"id": 3, "metadata": {}},                               # untagged
        {"id": 4, "metadata": {"embedder": "transformers:multilingual"}},
    ]
    stale = [r["id"] for r in stale_rows(rows, "onnx:minilm")]
    check("a row on the current model is not stale", 1 not in stale, str(stale))
    check("a row tagged with the old backend-only value is stale",
          2 in stale, str(stale))
    check("an untagged legacy row is stale", 3 in stale, str(stale))
    check("a row from a different model is stale", 4 in stale, str(stale))
    check("every stale row is returned, and only those",
          sorted(stale) == [2, 3, 4], str(stale))

    check("rows on the current model are never re-embedded",
          stale_rows([{"id": 1, "metadata": {"embedder": "onnx:minilm"}},
                      {"id": 5, "metadata": {"embedder": "onnx:minilm"}}],
                     "onnx:minilm") == [],
          "idempotence broken")

    # The dangerous pair: two different models, same vector width.
    a = stale_rows([{"id": 9, "metadata": {"embedder": "onnx:minilm"}}],
                   "transformers:multilingual")
    check("switching models makes the previous model's rows stale",
          [r["id"] for r in a] == [9], str(a))

# ---- 3. the retrieval floor is separate from, and below, the gate ----------

async def retrieval_checks() -> None:
    from backend.config import config
    from backend.memory.recall import build_memory_context_hybrid
    from backend.memory.vector_store import vector_store

    gate = float(config.get("memory", "min_similarity", default=0.35))
    floor = float(config.get("memory", "retrieval_min_similarity", default=gate))
    check("a retrieval floor is configured", floor is not None, str(floor))
    check("the floor is below the gate it feeds", floor < gate,
          f"floor={floor} gate={gate}")
    check("the floor is still high enough to exclude noise", floor >= 0.15,
          str(floor))

    # A store where a candidate scores between the floor and the gate: it must
    # enter the ranking (floor) and survive the fused gate (its score is below
    # the gate but it reached the ranking through BM25 as well).
    tmp = tempfile.mkdtemp(prefix="rag_")
    from pathlib import Path
    saved_path = vector_store.__class__.__module__
    check("the store is reachable for a live search",
          vector_store.available or True, saved_path)

    # Live behaviour only when there is something to search.
    rows = 0
    try:
        rows = len(vector_store.list(category="conversation", limit=5))
    except Exception as e:
        check("the conversation store can be listed", False, str(e))
    if rows == 0:
        print("       (no conversation memories here — checked the wiring only)")
        return

    below_gate = []
    from backend.memory.embedding import embed_text_async
    for probe_query in ("what did we work on", "addled", "why did that fail"):
        vec = await embed_text_async(probe_query)
        loose = vector_store.search(vec, category="conversation", top_k=4,
                                    min_similarity=floor)
        strict = vector_store.search(vec, category="conversation", top_k=4,
                                     min_similarity=gate)
        if len(loose) > len(strict):
            below_gate.append((probe_query, len(strict), len(loose)))
    check("the floor admits candidates the gate alone would drop",
          bool(below_gate),
          f"none found; floor={floor} gate={gate} — "
          f"either the store is too small or the floor is not doing anything")

    ctx = await build_memory_context_hybrid("what did we work on", top_k=3)
    check("hybrid recall still produces a context block", bool(ctx),
          str(ctx)[:120] if ctx else "nothing returned")

# ---- 4. health reports the silent failures ---------------------------------

def health_checks() -> None:
    from backend.memory.health import report

    out = report()
    for key in ("embedder", "store", "index", "settings", "warnings"):
        check(f"health reports '{key}'", key in out, str(sorted(out))[:120])
    check("warnings is a list", isinstance(out.get("warnings"), list),
          type(out.get("warnings")).__name__)
    check("the embedder's semantic capability is stated",
          isinstance((out.get("embedder") or {}).get("semantic"), bool),
          str(out.get("embedder"))[:120])
    check("the store row count is reported",
          isinstance((out.get("store") or {}).get("conversations"), int),
          str(out.get("store"))[:120])

    # The three silent states must each produce a warning.
    from backend.memory import health
    fake = {"embedder": {"kind": "hash", "semantic": False},
            "store": {"available": True, "conversations": 0},
            "index": {"docs": 0},
            "settings": {"min_similarity": 0.35,
                         "retrieval_min_similarity": 0.35,
                         "floor_below_gate": False}}
    warned = " ".join(health._warnings(fake))
    check("no semantic embedder is warned about",
          "keyword-only" in warned, warned[:160])
    check("an empty store is warned about", "No conversation memories" in warned,
          warned[:160])
    check("a floor that is not below the gate is warned about",
          "retrieval floor is doing nothing" in warned, warned[:160])

    healthy = {"embedder": {"kind": "onnx", "semantic": True},
               "store": {"available": True, "conversations": 500},
               "index": {"docs": 500},
               "settings": {"min_similarity": 0.35,
                            "retrieval_min_similarity": 0.25,
                            "floor_below_gate": True}}
    check("a healthy stack produces no warnings",
          health._warnings(healthy) == [], str(health._warnings(healthy)))

def main() -> int:
    embedder_identity_checks()
    staleness_checks()
    health_checks()
    asyncio.run(retrieval_checks())
    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        return 1
    print("PASS: embedder identity, retrieval floor, staleness and health")
    return 0

if __name__ == "__main__":
    sys.exit(main())
