"""
Typed relations between memories — and between memories and files.

Every memory store in Addled (facts, triples, conversation memories, session
summaries, journal days, wiki pages) is an independent flat collection keyed by
its own autoincrement id. Nothing could say "this fact came from that
conversation" or "this note is about that file".

This module is one edge table that connects any two items regardless of which
store they live in. Items are addressed by ``(kind, id)`` where ``id`` is always
a string, so a fact id, a file path and a wiki slug are addressable the same way.
Everything else — the dashboard graph, memory expansion during recall, the wiki's
backlinks — reads from here.

Design notes:
  * ``file`` targets are normalised to an absolute, case-folded path so the same
    file mentioned two ways is one node.
  * Links are directional, but :meth:`LinkStore.neighbors` walks both ways, so a
    caller asking "what is related to this?" need not know the stored direction.
  * Deleting a memory must call :func:`forget`, or the graph fills with edges
    pointing at rows that no longer exist.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("addled.memory.links")

DB_PATH: Path = Path(__file__).parent / "links.db"

# What a link endpoint can be.
KINDS = ("fact", "triple", "memory", "summary", "journal", "wiki", "file")

# The relation vocabulary. Closing it keeps the graph queryable and stops the
# model inventing a new relation name on every call.
RELATIONS = (
    "relates_to",    # generic association
    "same_as",       # duplicate / alias of
    "supersedes",    # newer version replaces the older one
    "contradicts",   # the two disagree
    "derived_from",  # provenance: this was produced from that
    "mentions",      # text refers to the target
    "part_of",       # contained by
    "sourced_from",  # a citation
    "documents",     # this explains that
    "links_to",      # an explicit [[wiki link]]
)

_MAX_NOTE = 200


def _norm_file(path: str) -> str:
    """Absolute, case-folded path so one file is one node."""
    try:
        expanded = os.path.expanduser(str(path).strip().strip('"'))
        return os.path.normcase(os.path.abspath(expanded))
    except Exception:
        return str(path).strip()


def normalise_ref(kind: str, ref_id) -> tuple[str, str]:
    """Validate a reference and return it ready to store."""
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"unknown memory kind '{kind}' (expected {KINDS})")
    text = str(ref_id if ref_id is not None else "").strip()
    if not text:
        raise ValueError("a link needs a non-empty id")
    if kind == "file":
        text = _norm_file(text)
    return kind, text


class LinkStore:
    """The edge table. One SQLite file, WAL mode."""

    def __init__(self, path: Path = DB_PATH):
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._connect()

    # -- plumbing ----------------------------------------------------------

    def _connect(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path),
                                         check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    src_kind TEXT NOT NULL,
                    src_id   TEXT NOT NULL,
                    rel      TEXT NOT NULL,
                    dst_kind TEXT NOT NULL,
                    dst_id   TEXT NOT NULL,
                    ts       REAL NOT NULL,
                    source   TEXT NOT NULL DEFAULT 'agent',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    note     TEXT,
                    meta     TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_links_edge
                    ON links(src_kind, src_id, rel, dst_kind, dst_id);
                CREATE INDEX IF NOT EXISTS idx_links_src
                    ON links(src_kind, src_id);
                CREATE INDEX IF NOT EXISTS idx_links_dst
                    ON links(dst_kind, dst_id);
                CREATE INDEX IF NOT EXISTS idx_links_rel ON links(rel);
            """)
            self._conn.commit()
        except Exception as e:
            log.warning("link store unavailable (%s) — relations disabled", e)
            self._conn = None

    @property
    def available(self) -> bool:
        return self._conn is not None

    # -- writes ------------------------------------------------------------

    def link(self, src_kind: str, src_id, rel: str, dst_kind: str, dst_id,
             source: str = "agent", confidence: float = 1.0,
             note: str = "", meta: dict | None = None) -> int | None:
        """Create or refresh an edge. Returns the link id, or None."""
        if self._conn is None:
            return None
        try:
            s_kind, s_id = normalise_ref(src_kind, src_id)
            d_kind, d_id = normalise_ref(dst_kind, dst_id)
        except ValueError as e:
            log.debug("link rejected: %s", e)
            return None
        rel = str(rel or "relates_to").strip().lower()
        if rel not in RELATIONS:
            rel = "relates_to"
        if s_kind == d_kind and s_id == d_id:
            return None  # no self-loops
        blob = json.dumps(meta, ensure_ascii=False) if meta else None
        try:
            with self._conn:
                self._conn.execute(
                    """INSERT INTO links (src_kind, src_id, rel, dst_kind,
                                          dst_id, ts, source, confidence, note,
                                          meta)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(src_kind, src_id, rel, dst_kind, dst_id)
                       DO UPDATE SET ts=excluded.ts, source=excluded.source,
                                     confidence=excluded.confidence,
                                     note=COALESCE(excluded.note, links.note),
                                     meta=COALESCE(excluded.meta, links.meta)""",
                    (s_kind, s_id, rel, d_kind, d_id, time.time(),
                     str(source or "agent"), float(confidence),
                     (note or "")[:_MAX_NOTE] or None, blob))
            row = self._conn.execute(
                """SELECT id FROM links WHERE src_kind=? AND src_id=? AND rel=?
                   AND dst_kind=? AND dst_id=?""",
                (s_kind, s_id, rel, d_kind, d_id)).fetchone()
            return int(row[0]) if row else None
        except Exception as e:
            log.debug("link write failed: %s", e)
            return None

    def unlink(self, link_id) -> bool:
        if self._conn is None:
            return False
        try:
            with self._conn:
                cur = self._conn.execute("DELETE FROM links WHERE id=?",
                                         (link_id,))
            return cur.rowcount > 0
        except Exception as e:
            log.debug("unlink failed: %s", e)
            return False

    def forget(self, kind: str, ref_id) -> int:
        """Remove every edge touching one item. Call when it is deleted."""
        if self._conn is None:
            return 0
        try:
            k, i = normalise_ref(kind, ref_id)
        except ValueError:
            return 0
        try:
            with self._conn:
                cur = self._conn.execute(
                    """DELETE FROM links
                       WHERE (src_kind=? AND src_id=?)
                          OR (dst_kind=? AND dst_id=?)""",
                    (k, i, k, i))
            return cur.rowcount
        except Exception as e:
            log.debug("forget failed: %s", e)
            return 0

    def forget_all(self, kind: str) -> int:
        if self._conn is None:
            return 0
        try:
            with self._conn:
                cur = self._conn.execute(
                    "DELETE FROM links WHERE src_kind=? OR dst_kind=?",
                    (kind, kind))
            return cur.rowcount
        except Exception:
            return 0

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _row_to_link(row, direction: str) -> dict:
        (_id, s_kind, s_id, rel, d_kind, d_id, ts, source, confidence,
         note, meta) = row
        try:
            extras = json.loads(meta) if meta else {}
        except (TypeError, ValueError):
            extras = {}
        # Present the *other* end so callers can render "related to X" without
        # knowing which side it was stored on.
        other = (d_kind, d_id) if direction == "out" else (s_kind, s_id)
        return {
            "id": int(_id),
            "rel": rel,
            "direction": direction,
            "kind": other[0],
            "ref_id": other[1],
            "src": {"kind": s_kind, "id": s_id},
            "dst": {"kind": d_kind, "id": d_id},
            "ts": ts,
            "source": source,
            "confidence": confidence,
            "note": note or "",
            "meta": extras,
        }

    def neighbors(self, kind: str, ref_id, direction: str = "both",
                  relations: list[str] | None = None,
                  limit: int = 100) -> list[dict]:
        """Edges touching one item, from either side by default."""
        if self._conn is None:
            return []
        try:
            k, i = normalise_ref(kind, ref_id)
        except ValueError:
            return []
        clauses, params = [], []
        if direction in ("out", "both"):
            clauses.append("(src_kind=? AND src_id=?)")
            params += [k, i]
        if direction in ("in", "both"):
            clauses.append("(dst_kind=? AND dst_id=?)")
            params += [k, i]
        where = " OR ".join(clauses) if clauses else "0"
        if relations:
            placeholders = ",".join("?" for _ in relations)
            where = f"({where}) AND rel IN ({placeholders})"
            params += [str(r).lower() for r in relations]
        sql = (f"SELECT id, src_kind, src_id, rel, dst_kind, dst_id, ts, source, "
               f"confidence, note, meta FROM links WHERE {where} "
               f"ORDER BY confidence DESC, ts DESC LIMIT ?")
        params.append(int(limit))
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except Exception as e:
            log.debug("neighbor query failed: %s", e)
            return []
        return [self._row_to_link(r, "out" if (r[1] == k and r[2] == i) else "in")
                for r in rows]

    def related(self, kind: str, ref_id, depth: int = 1,
                limit: int = 200) -> list[dict]:
        """Items reachable from one item, breadth-first, deduplicated.

        Each result carries the ``depth`` it was found at and the ``via`` edge,
        which is what makes "why is this related?" answerable.
        """
        if self._conn is None:
            return []
        try:
            start = normalise_ref(kind, ref_id)
        except ValueError:
            return []
        depth = max(1, min(int(depth), 4))
        seen = {start}
        frontier = [start]
        results: list[dict] = []
        for level in range(1, depth + 1):
            next_frontier = []
            for node_kind, node_id in frontier:
                for edge in self.neighbors(node_kind, node_id,
                                           limit=max(20, limit // 2)):
                    ref = (edge["kind"], edge["ref_id"])
                    if ref in seen:
                        continue
                    seen.add(ref)
                    results.append({
                        "kind": ref[0], "ref_id": ref[1], "depth": level,
                        "via": {"rel": edge["rel"], "from_kind": node_kind,
                                "from_id": node_id,
                                "direction": edge["direction"]},
                        "label": edge.get("note") or "",
                        "source": edge["source"],
                        "confidence": edge["confidence"],
                    })
                    next_frontier.append(ref)
                    if len(results) >= limit:
                        return results
            frontier = next_frontier
            if not frontier:
                break
        return results

    def files(self, limit: int = 200) -> list[dict]:
        """Every file memories point at, with how many point at it."""
        if self._conn is None:
            return []
        try:
            rows = self._conn.execute(
                """SELECT dst_id, COUNT(*) AS n, MAX(ts) AS last
                   FROM links WHERE dst_kind='file'
                   GROUP BY dst_id ORDER BY n DESC, last DESC LIMIT ?""",
                (int(limit),)).fetchall()
            extra = dict(self._conn.execute(
                """SELECT dst_id, COUNT(*) FROM links
                   WHERE src_kind='file' GROUP BY dst_id""").fetchall())
        except Exception:
            return []
        out = [{"path": r[0], "links": int(r[1]), "last": r[2], "exists":
                os.path.exists(r[0])} for r in rows]
        for path, count in extra.items():
            if not any(o["path"] == path for o in out):
                out.append({"path": path, "links": 0, "last": 0,
                            "exists": os.path.exists(path), "outgoing": int(count)})
        return out

    def stats(self) -> dict:
        if self._conn is None:
            return {"available": False, "links": 0, "by_kind": {}, "by_rel": {}}
        try:
            total = self._conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            by_rel = dict(self._conn.execute(
                "SELECT rel, COUNT(*) FROM links GROUP BY rel").fetchall())
            by_kind: dict[str, int] = {}
            for kind, count in self._conn.execute(
                    """SELECT src_kind, COUNT(*) FROM links GROUP BY src_kind
                       UNION ALL
                       SELECT dst_kind, COUNT(*) FROM links GROUP BY dst_kind"""):
                by_kind[kind] = by_kind.get(kind, 0) + count
            files = self._conn.execute(
                "SELECT COUNT(DISTINCT dst_id) FROM links WHERE dst_kind='file'"
            ).fetchone()[0]
        except Exception:
            return {"available": True, "links": 0, "by_kind": {}, "by_rel": {}}
        return {"available": True, "links": int(total), "by_kind": by_kind,
                "by_rel": by_rel, "files": int(files)}

    def export(self, limit: int = 500) -> list[dict]:
        """Raw edges, for the dashboard graph view."""
        if self._conn is None:
            return []
        try:
            rows = self._conn.execute(
                """SELECT id, src_kind, src_id, rel, dst_kind, dst_id, ts,
                          source, confidence, note, meta
                   FROM links ORDER BY ts DESC LIMIT ?""", (int(limit),)
            ).fetchall()
        except Exception:
            return []
        return [self._row_to_link(r, "out") for r in rows]

    # -- integrity ---------------------------------------------------------

    def prune(self) -> dict:
        """Drop edges whose endpoints no longer exist.

        Without this the graph accumulates references to deleted facts, triples
        and files, which then surface as phantom nodes in the dashboard.
        """
        if self._conn is None:
            return {"available": False, "checked": 0, "removed": 0}
        try:
            rows = self._conn.execute(
                "SELECT id, src_kind, src_id, dst_kind, dst_id FROM links"
            ).fetchall()
        except Exception:
            return {"available": False, "checked": 0, "removed": 0}
        gone = [link_id for link_id, s_kind, s_id, d_kind, d_id in rows
                if not _exists(s_kind, s_id) or not _exists(d_kind, d_id)]
        if gone:
            try:
                with self._conn:
                    self._conn.executemany("DELETE FROM links WHERE id=?",
                                           [(i,) for i in gone])
            except Exception as e:
                log.debug("prune delete failed: %s", e)
        return {"available": True, "checked": len(rows), "removed": len(gone)}


def _exists(kind: str, ref_id: str) -> bool:
    """Does the thing this edge points at still exist?

    Deliberately fail-open: if a store cannot be read we keep the edge rather
    than deleting relations the user may still want.
    """
    try:
        if kind == "file":
            return os.path.exists(ref_id)
        if kind == "fact":
            from backend.memory import facts
            return any(str(f.get("id")) == str(ref_id)
                       for f in facts.get_facts(limit=10000))
        if kind == "triple":
            from backend.memory.knowledge_graph import kg
            if not kg.available:
                return True
            return bool(kg._conn.execute("SELECT 1 FROM triples WHERE id=?",
                                         (int(ref_id),)).fetchone())
        if kind == "memory":
            from backend.memory.vector_store import vector_store
            if not vector_store.available:
                return True
            return bool(vector_store._conn.execute(
                "SELECT 1 FROM vectors WHERE id=?", (int(ref_id),)).fetchone())
        if kind == "summary":
            from backend.memory.session_summary import _load
            return 0 <= int(ref_id) < len(_load())
        if kind == "journal":
            from backend.memory import journal
            return bool((journal.get_day(ref_id) or {}).get("entries"))
        if kind == "wiki":
            from backend.wiki import store as wiki_store
            return wiki_store.exists(ref_id)
    except Exception as e:
        log.debug("existence check for %s/%s failed: %s", kind, ref_id, e)
        return True
    return True


link_store = LinkStore()
