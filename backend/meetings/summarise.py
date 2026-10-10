"""Summarise a transcript into decisions, actions and open questions.

Map-reduce, not one prompt. A one-hour meeting is 8,000-12,000 words, which
does not fit in a prompt that also has to hold the instructions, and the models
Addled uses have an 8k context on the local path. Sending it whole means the
provider truncates — silently, from the caller's side — and the summary is then
of the first few minutes only, with nothing saying so.

So the transcript is split into blocks that keep their timestamps, each block is
summarised on its own, and the block summaries are reduced into one result. The
timestamps survive the first stage because they are the anchor a reader uses to
check a claim ("was that before or after the budget was agreed?"), and a summary
without them cannot be verified against the transcript.

This module is deliberately separate from the skill that calls it: the blocking
and parsing logic is where the bugs live, and it is testable without a model.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("addled.meetings.summarise")

# ~2,000 tokens of transcript per block, by the same ~4 chars/token estimate the
# rest of the codebase uses. Small enough to leave room for instructions in an
# 8k context, large enough that a 10-minute block is one call rather than five.
BLOCK_CHARS = 8_000

# How long a single summary call may take before it is treated as failed. A
# summarise that never returns must surface as "the summariser did not answer",
# never as a spinner — the same rule the plan sets for live capture.
CALL_TIMEOUT_S = 180.0

_INSTRUCTIONS = (
    "You are summarising part {index} of {total} of a meeting transcript.\n"
    "Return ONLY a JSON object with these keys:\n"
    '  "summary": 2-4 sentences on what was discussed.\n'
    '  "decisions": array of strings — things that were actually decided.\n'
    '  "actions": array of objects {{"what": str, "who": str}} — things '
    "someone has to do. Use \"\" for who if the transcript does not say.\n"
    '  "open_questions": array of strings — raised but not settled.\n'
    "Keep the MM:SS timestamps that appear in the transcript next to any point "
    "you take from it. Do not invent anything that is not in the text. If a "
    "list is empty, use an empty array."
)

_REDUCE_INSTRUCTIONS = (
    "You are combining summaries of the consecutive parts of ONE meeting into a "
    "single result. Merge duplicates, keep the earliest timestamp for anything "
    "mentioned more than once, and drop nothing that was decided.\n"
    "Return ONLY a JSON object with the same keys:\n"
    '  "summary": 3-6 sentences on the whole meeting.\n'
    '  "decisions": array of strings.\n'
    '  "actions": array of objects {{"what": str, "who": str}}.\n'
    '  "open_questions": array of strings.'
)


def _looks_like_transcript(segments) -> bool:
    return bool(segments) and all(
        isinstance(s, dict) and ("at" in s or "start" in s) for s in segments)


def blocks(transcript: str, segments=None, *, block_chars: int = BLOCK_CHARS
           ) -> list[dict]:
    """Split a transcript into summarisable blocks, keeping timestamps.

    Blocks follow the segment boundaries where there are segments, because a
    block that ends mid-sentence loses the context that makes the next block
    readable. Without segments it falls back to splitting on paragraph breaks
    and then on length, so a wall of text still gets blocked.

    Each block is ``{"index", "total", "text", "from", "to"}`` where ``from``
    and ``to`` are the MM:SS stamps the block covers, so a summary can say which
    part of the meeting it came from.
    """
    text = (transcript or "").strip()
    if not text:
        return []

    out: list[dict] = []

    if _looks_like_transcript(segments):
        cur: list[str] = []
        size = 0
        start_at = None
        end_at = None
        for s in segments:
            piece = str(s.get("text", "") or "").strip()
            if not piece:
                continue
            stamp = str(s.get("at") or "")
            line = f"[{stamp}] {piece}" if stamp else piece
            if cur and size + len(line) > block_chars:
                out.append({"text": "\n".join(cur), "from": start_at or "",
                            "to": end_at or ""})
                cur, size, start_at = [], 0, None
            if start_at is None:
                start_at = stamp
            end_at = stamp
            cur.append(line)
            size += len(line) + 1
        if cur:
            out.append({"text": "\n".join(cur), "from": start_at or "",
                        "to": end_at or ""})
    else:
        # No timestamps available. Split on blank lines first, then hard-split
        # anything still too long, so one giant transcript cannot become one
        # giant prompt.
        chunks: list[str] = []
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if not para:
                continue
            while len(para) > block_chars:
                cut = para.rfind(". ", 0, block_chars)
                if cut < block_chars // 2:
                    cut = block_chars
                else:
                    cut += 1
                chunks.append(para[:cut].strip())
                para = para[cut:].strip()
            if para:
                chunks.append(para)
        cur, size = [], 0
        for c in chunks:
            if cur and size + len(c) > block_chars:
                out.append({"text": "\n\n".join(cur), "from": "", "to": ""})
                cur, size = [], 0
            cur.append(c)
            size += len(c) + 2
        if cur:
            out.append({"text": "\n\n".join(cur), "from": "", "to": ""})

    total = len(out)
    for i, b in enumerate(out):
        b["index"] = i + 1
        b["total"] = total
    return out


def _parse_json_object(text: str) -> dict | None:
    """Pull the JSON object out of a model reply.

    Models wrap JSON in prose or in a fenced block even when told not to, so the
    first balanced object is taken rather than requiring the whole reply to
    parse. A reply with no object returns None, which the caller reports as a
    failure rather than as an empty summary — the two are very different and
    conflating them is how "no decisions were found" becomes indistinguishable
    from "the model did not answer".
    """
    if not text:
        return None
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # First balanced {...}
    start = s.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        break
        start = s.find("{", start + 1)
    return None


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _shape(obj: dict) -> dict:
    """Normalise a model reply into the fields the store expects."""
    decisions = []
    for d in _as_list(obj.get("decisions")):
        s = str(d).strip() if not isinstance(d, dict) else str(d).strip()
        if s:
            decisions.append(s)

    actions = []
    for a in _as_list(obj.get("actions")):
        if isinstance(a, dict):
            what = str(a.get("what") or a.get("action") or a.get("task") or "").strip()
            who = str(a.get("who") or a.get("owner") or "").strip()
        else:
            what, who = str(a).strip(), ""
        if what:
            actions.append({"what": what, "who": who})

    questions = [str(q).strip() for q in _as_list(obj.get("open_questions"))
                 if str(q).strip()]
    return {
        "summary": str(obj.get("summary") or "").strip(),
        "decisions": decisions,
        "actions": actions,
        "open_questions": questions,
    }


def _merge(parts: list[dict]) -> dict:
    """Combine block results in code, as the reduce stage's fallback.

    Used when the reduce call itself fails, so a failed reduce costs the merged
    resolution of the pieces rather than the whole summary. Cheap and lossless:
    every decision and action is kept, duplicates removed on their text.
    """
    summary = " ".join(p.get("summary", "").strip()
                       for p in parts if p.get("summary")).strip()
    decisions: list[str] = []
    actions: list[dict] = []
    questions: list[str] = []
    seen_d: set[str] = set()
    seen_a: set[str] = set()
    seen_q: set[str] = set()
    for p in parts:
        for d in p.get("decisions", []):
            if d.lower() not in seen_d:
                seen_d.add(d.lower())
                decisions.append(d)
        for a in p.get("actions", []):
            key = (a.get("what", "") + "|" + a.get("who", "")).lower()
            if key not in seen_a:
                seen_a.add(key)
                actions.append(a)
        for q in p.get("open_questions", []):
            if q.lower() not in seen_q:
                seen_q.add(q.lower())
                questions.append(q)
    return {"summary": summary, "decisions": decisions, "actions": actions,
            "open_questions": questions}


async def _ask(provider, prompt: str, *, timeout: float = CALL_TIMEOUT_S) -> str | None:
    """One model call. Returns the text, or None if it failed or timed out.

    None is a real answer here — "the summariser did not reply" — and is kept
    distinct from an empty string, which is a reply that said nothing.
    """
    import asyncio
    try:
        result = await asyncio.wait_for(
            provider.chat([{"role": "user", "content": prompt}],
                          max_tokens=1500, temperature=0.2),
            timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("summarise call did not answer within %.0fs", timeout)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("summarise call failed: %s", e)
        return None
    if not getattr(result, "ok", False):
        log.warning("summarise call returned an error: %s",
                    getattr(result, "error", "?"))
        return None
    return getattr(result, "response", "") or ""


async def summarise(transcript: str, *, provider=None, segments=None,
                    block_chars: int = BLOCK_CHARS,
                    timeout: float = CALL_TIMEOUT_S) -> dict:
    """Summarise a transcript. Returns a result dict, never raises.

    The result is ``{"success", "summary", "decisions", "actions",
    "open_questions", "blocks", "parts_ok", "partial", "error"}``.

    ``partial`` is the honest flag the plan asks for: True means only some
    blocks were summarised, and ``parts_ok``/``blocks`` say how many. A caller
    must show that rather than present a partial summary as the whole meeting.
    """
    text = (transcript or "").strip()
    if not text:
        return {"success": False, "error": "the transcript is empty",
                "summary": "", "decisions": [], "actions": [],
                "open_questions": [], "blocks": 0, "parts_ok": 0,
                "partial": False}

    if provider is None:
        from backend.providers.registry import get_provider
        provider = get_provider()
    if provider is None:
        return {"success": False, "error": "no model provider is configured",
                "summary": "", "decisions": [], "actions": [],
                "open_questions": [], "blocks": 0, "parts_ok": 0,
                "partial": False}

    blks = blocks(text, segments, block_chars=block_chars)

    # One block is the common case for a short meeting: summarise it directly
    # rather than paying for a reduce stage that would just reformat it.
    if len(blks) == 1:
        prompt = (_INSTRUCTIONS.format(index=1, total=1)
                  + "\n\nTRANSCRIPT:\n" + blks[0]["text"])
        raw = await _ask(provider, prompt, timeout=timeout)
        obj = _parse_json_object(raw) if raw is not None else None
        if obj is None:
            return {"success": False,
                    "error": ("the summariser did not answer"
                              if raw is None else
                              "the summariser did not return usable JSON"),
                    "summary": "", "decisions": [], "actions": [],
                    "open_questions": [], "blocks": 1,
                    "parts_ok": 0, "partial": False}
        out = _shape(obj)
        out.update({"success": True, "blocks": 1, "parts_ok": 1,
                    "partial": False, "error": None})
        return out

    parts: list[dict] = []
    failed: list[int] = []
    for b in blks:
        prompt = (_INSTRUCTIONS.format(index=b["index"], total=b["total"])
                  + f"\n\nTRANSCRIPT ({b['from']}–{b['to']}):\n" + b["text"])
        raw = await _ask(provider, prompt, timeout=timeout)
        obj = _parse_json_object(raw) if raw is not None else None
        if obj is None:
            failed.append(b["index"])
            continue
        part = _shape(obj)
        if b["from"]:
            part["_from"] = b["from"]
        parts.append(part)

    if not parts:
        return {"success": False,
                "error": ("none of the "
                          f"{len(blks)} transcript blocks could be summarised"),
                "summary": "", "decisions": [], "actions": [],
                "open_questions": [], "blocks": len(blks),
                "parts_ok": 0, "partial": False}

    merged = _merge(parts)

    # The reduce stage. If it fails the merged block results are returned
    # instead of nothing — a summary of every block, concatenated and
    # deduplicated, is worse prose than a reduce but is not lost work.
    joined = json.dumps([{k: v for k, v in p.items() if not k.startswith("_")}
                         for p in parts], ensure_ascii=False)
    raw = await _ask(provider, _REDUCE_INSTRUCTIONS + "\n\nPART SUMMARIES:\n"
                     + joined, timeout=timeout)
    obj = _parse_json_object(raw) if raw is not None else None
    if obj is not None:
        final = _shape(obj)
        # A reduce that loses everything the parts found is worse than not
        # reducing. Keep the parts' findings if the reduce came back empty.
        if not any(final[k] for k in ("decisions", "actions", "open_questions")):
            merged_extra = merged
            final["decisions"] = final["decisions"] or merged_extra["decisions"]
            final["actions"] = final["actions"] or merged_extra["actions"]
            final["open_questions"] = (final["open_questions"]
                                       or merged_extra["open_questions"])
    else:
        final = merged
        log.info("reduce stage failed; returning merged block summaries")

    partial = bool(failed)
    return {
        "success": True,
        "summary": final["summary"],
        "decisions": final["decisions"],
        "actions": final["actions"],
        "open_questions": final["open_questions"],
        "blocks": len(blks),
        "parts_ok": len(parts),
        "partial": partial,
        "failed_blocks": failed,
        "error": (f"{len(failed)} of {len(blks)} parts could not be "
                  "summarised" if partial else None),
    }
