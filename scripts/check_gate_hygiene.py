"""A permanent check that the gate's fixtures cannot certify themselves.

The gate failed twice in a row for reasons that had nothing to do with the
app: a previous run's answer was stored in the same conversation memory the
gate reads, so the model answered from it and no tool was needed. The
precondition also could not see it, and the gate reported the store clean.

Two properties are asserted here, and BOTH matter:

  1. it must RECOGNISE a poisoned store (else it certifies what it cannot see),
  2. it must NOT flag ordinary history (else 'cleaning' deletes real memory --
     a first attempt at these markers matched `got it` and `action items`, and
     would have destroyed two unrelated rows).

The false-positive direction is the one that is easy to skip and costly to get
wrong, so it is checked with real rows taken from the live store.
"""

import os
import subprocess
import sys

ROOT = r"C:\Users\Steru\orca\workspaces\Addled\glaucus"
os.environ.setdefault("ADDLED_DATA_DIR", os.path.join(os.environ.get("LOCALAPPDATA"), "Addled"))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, ".livetest"))

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print("  ok   %s" % label)
    else:
        fails.append("%s%s" % (label, (" -- " + detail) if detail else ""))
        print("  FAIL %s%s" % (label, (" -- " + detail) if detail else ""))


print("The gate can tell a poisoned store from a clean one")
try:
    import gate_actions as g
except Exception as exc:  # pragma: no cover - import-time failure is the point
    print("  FAIL cannot import the gate: %r" % (exc,))
    raise SystemExit(1)

# Scan only the marker TUPLES, not prose. `action items` legitimately appears
# in the gate's question and in the comment that warns against using it as a
# marker; scanning the whole file flagged those and produced a false failure.
# Read the gate's own source so this check cannot drift from its marker list.
src = open(os.path.join(ROOT, ".livetest", "gate_actions.py"),
           encoding="utf-8").read()

import re as _re

_tuples = _re.findall(r"markers = \((.*?)\)\s*$", src, _re.M | _re.S)
# The screen guard has its own `markers = (...)` list (strings a screen
# description must not leak). Different list, different job, so count only the
# FIXTURE markers: both of those name the planted meeting's own content.
_fixture_tuples = [t for t in _tuples if "phoenix" in t and "northwind" in t]
check("the gate defines its fixture markers as tuples",
      len(_fixture_tuples) == 2,
      "found %d fixture marker tuple(s), expected 2 (of %d total)"
      % (len(_fixture_tuples), len(_tuples)))

_generic = ('"got it"', '"let me pull up"', '"action items"')
# Strip comments first: the marker block carries a comment that warns AGAINST
# these words, and scanning it raw made this check flag its own advice.
_stripped = [_re.sub(r"#[^\n]*", "", t) for t in _fixture_tuples]
_bad = [g for g in _generic if any(g in t for t in _stripped)]
check("no generic English survives in the marker tuples",
      not _bad,
      "these would match real history and deleting them destroys memory: %r"
      % (_bad,))

check("every marker tuple mentions the planted meeting's own content",
      all(("cut over on the 14th" in t or "phoenix" in t) for t in _fixture_tuples),
      "a marker list has no distinctive string from the fixture")

# The planted-meeting strings must all be present in the planted meeting, or
# the precondition is keying on something the fixture never writes.
for needle in ("Phoenix migration review", "Cut over on the 14th",
               "pricing cap", "migration plan"):
    check("the fixture actually writes %r" % needle,
          needle.lower() in src.lower(),
          "the marker has no counterpart in the planted meeting")

# And the reverse: an unrelated sentence must not trip it.
import gate_actions as g  # noqa: E402

probe = ("Got it - split by category. I'm ready.\n\nBut I don't actually have "
         "the txt file yet. action items aside, here is the honest reasoning.")
check("an unrelated answer does not trip the precondition",
      not any(mk in probe.lower() for mk in (
          "phoenix", "cut over on the 14th", "pricing cap", "migration plan",
          "platform team", "migration review")),
      "an ordinary sentence would be flagged, and cleaning it destroys memory")

print()
if fails:
    print("%d FAILED" % len(fails))
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("All gate-hygiene checks passed.")
