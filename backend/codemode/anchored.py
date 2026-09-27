"""
Anchored edits — apply a change by locating exact text, not by rewriting a file.

Why this exists
---------------
`code.edit` used to hand the model the whole file and ask for the whole file
back. That has two failures that get worse as files get bigger:

1. **Truncation reads as deletion.** A reply cut off at the token cap comes back
   with the tail missing, and the diff says "you deleted everything after line
   200". The guard against it was a hard 12,000-character ceiling — above that,
   editing simply refused.
2. **Drift on untouched lines.** Asking a model to reproduce 800 lines it must
   not change is asking it to introduce 800 chances to change one anyway
   (whitespace, a "helpful" tidy, a renamed local). Review then has to read the
   whole file rather than the change.

An anchored edit carries only the change: the exact text to find, and what to
put there. Everything else is left byte-for-byte alone, so the diff *is* the
change, a truncated reply is caught (the anchor is missing), and there is no
size ceiling worth speaking of.

Matching is deliberately strict, in this order:
  1. exact substring (the common, unambiguous case)
  2. exact, ignoring trailing whitespace per line
  3. exact, ignoring leading whitespace per line (indentation-only drift)
  4. whitespace-insensitive (runs of whitespace compared as one space)

Step 4 is what lets a model that re-wraps a line still find its anchor. It is
last because it is the loosest, and the ambiguity check below always runs
before an edit is allowed to touch anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

@dataclass
class MatchResult:
    """Where an anchor was found, and how strictly."""
    found: bool
    start: int = -1
    end: int = -1
    mode: str = ""
    count: int = 0
    error: str = ""
    candidates: list[int] = field(default_factory=list)

def _runs_ws(text: str) -> str:
    """Collapse every run of whitespace to one space and strip the ends.

    Only used for the loosest comparison; the returned offsets are mapped back
    to the original string, never used to slice it.
    """
    return re.sub(r"\s+", " ", text).strip()

def _ws_normalised_with_map(text: str) -> tuple[str, list[int]]:
    """`_runs_ws(text)` plus, for each character of the result, its index in
    the original. The map is what makes a loose match usable: it says where in
    the real file the collapsed text came from.
    """
    out_chars: list[str] = []
    out_map: list[int] = []
    i, n = 0, len(text)
    # Skip leading whitespace.
    while i < n and text[i].isspace():
        i += 1
    prev_space = False
    while i < n:
        ch = text[i]
        if ch.isspace():
            prev_space = True
            i += 1
            continue
        if prev_space and out_chars:
            out_chars.append(" ")
            out_map.append(i)
        prev_space = False
        out_chars.append(ch)
        out_map.append(i)
        i += 1
    # Trailing whitespace was never appended, so the collapsed string is already
    # stripped at both ends.
    return "".join(out_chars), out_map

def find_anchor(haystack: str, needle: str) -> MatchResult:
    """Locate `needle` in `haystack`, strictest match first.

    Returns the offsets of the match and how it was found. `count` above 1 means
    the anchor is ambiguous: the caller must refuse rather than guess, because
    an edit applied to the wrong occurrence is worse than no edit.
    """
    if not needle:
        return MatchResult(False, error="Empty anchor — nothing to find.")
    if not haystack:
        return MatchResult(False, error="The file is empty.")

    # 1. exact
    first = haystack.find(needle)
    if first != -1:
        count = haystack.count(needle)
        return MatchResult(True, first, first + len(needle), "exact", count,
                           candidates=[i for i in _all_indices(haystack, needle)])

    # 2. ignore trailing whitespace per line
    res = _match_by_lines(haystack, needle, strip_trailing=True,
                          strip_leading=False)
    if res.found:
        return res

    # 3. ignore leading whitespace per line (indentation drift)
    res = _match_by_lines(haystack, needle, strip_trailing=True,
                          strip_leading=True)
    if res.found:
        return res

    # 4. whitespace-insensitive
    collapsed_needle = _runs_ws(needle)
    if collapsed_needle:
        collapsed_hay, index_map = _ws_normalised_with_map(haystack)
        pos = collapsed_hay.find(collapsed_needle)
        if pos != -1:
            hits = [m.start() for m in
                    re.finditer(re.escape(collapsed_needle), collapsed_hay)]
            start = index_map[pos]
            end_collapsed = pos + len(collapsed_needle) - 1
            end = index_map[end_collapsed] + 1
            return MatchResult(True, start, end, "whitespace-insensitive",
                               len(hits), candidates=hits)

    return MatchResult(False, error=(
        "The text to replace was not found in the file. Re-read the file and "
        "copy the anchor exactly as it appears, including its indentation."))

def _all_indices(haystack: str, needle: str):
    start = haystack.find(needle)
    while start != -1:
        yield start
        start = haystack.find(needle, start + 1)

def _match_by_lines(haystack: str, needle: str, strip_trailing: bool,
                    strip_leading: bool) -> MatchResult:
    """Line-window match: compare each candidate window of the same line count.

    Not a regex — the needle is arbitrary text and may contain characters that
    would need escaping, and a regex built from a file's contents is a footgun.
    """
    hay_lines = haystack.splitlines(keepends=True)
    needle_lines = needle.splitlines()

    def norm(line: str) -> str:
        s = line.rstrip("\r\n")
        if strip_trailing:
            s = s.rstrip()
        if strip_leading:
            s = s.lstrip()
        return s

    want = [norm(l) for l in needle_lines]
    if not want or all(w == "" for w in want):
        return MatchResult(False)

    hits: list[int] = []
    window = len(needle_lines)
    for i in range(0, len(hay_lines) - window + 1):
        candidate = [norm(hay_lines[i + j]) for j in range(window)]
        if candidate == want:
            hits.append(i)

    if not hits:
        return MatchResult(False)

    # Offsets of the matched lines, in the original string.
    def line_start(idx: int) -> int:
        return sum(len(l) for l in hay_lines[:idx])

    first = hits[0]
    start = line_start(first)
    end = line_start(first + window)
    mode = "indentation-insensitive" if strip_leading else "trailing-whitespace"
    return MatchResult(True, start, end, mode, len(hits), candidates=hits)

def apply_anchor(original: str, anchor: str, replacement: str,
                 occurrence: int | None = None,
                 replace_all: bool = False) -> tuple[str, MatchResult]:
    """Return (new_text, match). `new_text` is unchanged when the match failed.

    Ambiguity is refused unless the caller says which occurrence or asks for all
    of them — `occurrence` is 1-based, the way a person would count.
    """
    # A replacement identical to its anchor changes nothing, so reporting it as
    # applied would be a false success the user cannot see through. Refused here
    # as well as in `parse_edit_ops`, because an EditOp can be built by hand.
    if anchor == replacement:
        return original, MatchResult(
            False, error="The replacement is identical to the anchor — that "
                         "would change nothing.")

    match = find_anchor(original, anchor)
    if not match.found:
        return original, match

    if replace_all:
        # Only the strict path can replace every hit safely: the loose modes
        # cannot guarantee the replacements line up with disjoint originals.
        if match.mode != "exact":
            return original, MatchResult(
                False, error=("Replacing every occurrence is only offered for "
                              "an exact match; this one was found "
                              f"{match.mode}, which may cover overlapping "
                              "text. Use a single replacement."))
        new_text = original.replace(anchor, replacement)
        return new_text, MatchResult(True, mode="exact-all", count=match.count)

    if occurrence is not None:
        if occurrence < 1 or occurrence > match.count:
            return original, MatchResult(
                False, error=(f"Occurrence {occurrence} was asked for but the "
                              f"anchor appears {match.count} time(s)."))
        if match.mode == "exact":
            positions = list(_all_indices(original, anchor))
            at = positions[occurrence - 1]
            new_text = original[:at] + replacement + original[at + len(anchor):]
            return new_text, MatchResult(True, at, at + len(anchor), "exact",
                                         1)
        # A loose match with a chosen occurrence: use the candidate list.
        if occurrence - 1 >= len(match.candidates):
            return original, MatchResult(False, error="Occurrence out of range.")
        if match.mode == "whitespace-insensitive":
            return _apply_loose_occurrence(original, anchor, replacement,
                                           occurrence)
        line = match.candidates[occurrence - 1]
        res = _apply_line_occurrence(original, anchor, replacement, line)
        return res

    if match.count > 1:
        return original, MatchResult(
            False, error=(f"The text to replace appears {match.count} times in "
                          "the file. Include more surrounding lines so it is "
                          "unambiguous, or pass `occurrence` to say which one."),
            count=match.count, candidates=match.candidates)

    start, end = match.start, match.end
    return original[:start] + replacement + original[end:], match

def _apply_line_occurrence(original: str, anchor: str, replacement: str,
                           line_index: int) -> tuple[str, MatchResult]:
    hay_lines = original.splitlines(keepends=True)
    window = len(anchor.splitlines())
    start = sum(len(l) for l in hay_lines[:line_index])
    end = sum(len(l) for l in hay_lines[:line_index + window])
    # Keep the original line endings on the replacement when it is a single line
    # and the region ended with one: dropping the newline would join two lines.
    repl = replacement
    if window and hay_lines[line_index + window - 1].endswith(("\n", "\r")) \
            and not repl.endswith(("\n", "\r")):
        repl = repl + ("\r\n" if hay_lines[line_index + window - 1]
                       .endswith("\r\n") else "\n")
    return original[:start] + repl + original[end:], MatchResult(
        True, start, end, "line-window", 1)

def _apply_loose_occurrence(original: str, anchor: str, replacement: str,
                            occurrence: int) -> tuple[str, MatchResult]:
    """Apply the Nth whitespace-insensitive match."""
    collapsed_needle = _runs_ws(anchor)
    if not collapsed_needle:
        return original, MatchResult(False, error="Empty anchor.")
    collapsed_hay, index_map = _ws_normalised_with_map(original)
    hits = [m.start() for m in
            re.finditer(re.escape(collapsed_needle), collapsed_hay)]
    if occurrence > len(hits):
        return original, MatchResult(False, error="Occurrence out of range.")
    pos = hits[occurrence - 1]
    start = index_map[pos]
    end = index_map[pos + len(collapsed_needle) - 1] + 1
    return original[:start] + replacement + original[end:], MatchResult(
        True, start, end, "whitespace-insensitive", 1)

@dataclass
class EditOp:
    """One anchored change, as the model reports it."""
    anchor: str
    replacement: str
    occurrence: int | None = None
    replace_all: bool = False

def parse_edit_ops(payload: dict) -> tuple[list[EditOp], str]:
    """Read an edit list from a model's JSON, tolerating the usual spelling
    variants. Returns (ops, error). `error` is non-empty when nothing usable
    was found, said plainly enough to put in a tool result.
    """
    if not isinstance(payload, dict):
        return [], "The edit was not a JSON object."
    raw = (payload.get("edits") or payload.get("changes")
           or payload.get("operations"))
    if raw is None:
        # A single edit given at the top level.
        if "anchor" in payload or "find" in payload or "old" in payload:
            raw = [payload]
        else:
            return [], ("No 'edits' array in the reply. Expected "
                        '{"edits": [{"anchor": "...", "replacement": "..."}]}.')
    if not isinstance(raw, list) or not raw:
        return [], "The 'edits' array was empty."

    ops: list[EditOp] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], f"Edit {i + 1} was not an object."
        anchor = (item.get("anchor") or item.get("find") or item.get("old")
                  or item.get("search"))
        replacement = item.get("replacement")
        if replacement is None:
            replacement = item.get("replace")
        if replacement is None:
            replacement = item.get("new")
        if anchor is None:
            return [], f"Edit {i + 1} has no anchor ('find'/'old'/...)."
        if replacement is None:
            return [], f"Edit {i + 1} has no replacement ('new'/'replace'/...)."
        if anchor == replacement:
            return [], (f"Edit {i + 1} replaces text with itself; that would "
                        "change nothing.")
        occ = item.get("occurrence") or item.get("nth")
        try:
            occ = int(occ) if occ not in (None, "") else None
        except (TypeError, ValueError):
            occ = None
        ops.append(EditOp(str(anchor), str(replacement), occ,
                          bool(item.get("replaceAll")
                               or item.get("replace_all"))))
    return ops, ""

def apply_edits(original: str, ops: list[EditOp]) -> tuple[str, list[dict]]:
    """Apply ops in order. Returns (new_text, report).

    Each op is applied to the result of the previous one, so an edit may anchor
    on text an earlier edit just introduced — which is how a rename followed by
    a use of the new name has to work. A failed op stops the run and leaves the
    text as it was up to that point; `report` says which succeeded.
    """
    text = original
    report: list[dict] = []
    for i, op in enumerate(ops):
        new_text, match = apply_anchor(
            text, op.anchor, op.replacement,
            occurrence=op.occurrence, replace_all=op.replace_all)
        entry = {
            "index": i,
            "anchor": op.anchor[:120] + ("…" if len(op.anchor) > 120 else ""),
            "ok": bool(match.found),
            "mode": match.mode,
        }
        if not match.found:
            # Stop and hand back the text as it stood BEFORE this op. Earlier
            # ops succeeded and are kept, because they are reported as ok and
            # the user is shown the diff for exactly what was applied — silently
            # reverting them would make the report a lie.
            entry["error"] = match.error or "Anchor not found."
            report.append(entry)
            return text, report
        text = new_text
        report.append(entry)
    return text, report
