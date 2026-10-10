"""Meeting notes: transcript timestamps and the meeting store.

Two phases of the meeting-notes plan, and the reasons each is asserted:

  * `transcribe_file` joined `s.text` and threw `s.start`/`s.end` away. Whisper
    produces the times for free; without them a transcript cannot say when
    anything was said, which is most of what makes it useful as notes. The join
    itself is a contract several callers read, so it must survive unchanged.
  * The meeting store is the shape every later phase writes into. A wrong shape
    is expensive to change once transcripts and summaries exist in it.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_meetings.py
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Isolated before anything imports the store, so the real meetings directory is
# never touched. This suite creates and deletes meetings.
_TMP = tempfile.mkdtemp(prefix="addled_meetings_check_")
os.environ["ADDLED_DATA_DIR"] = _TMP

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f" — {detail}" if not cond and detail else ""))


print("Transcript timestamps")

from backend.voice.stt import _collect_segments, _clock  # noqa: E402


class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class _Info:
    language = "en"


_segs = [_Seg(0.0, 2.4, " Hello there. "),
         _Seg(2.4, 8.0, "This is a meeting."),
         _Seg(3661.5, 3665.0, "Past an hour.")]
_r = _collect_segments(_segs, _Info())

check("the joined text is unchanged from the old join",
      _r["text"] == " ".join(s.text.strip() for s in _segs),
      repr(_r["text"]))
check("every spoken segment is kept", len(_r["segments"]) == 3,
      f"{len(_r['segments'])} segments")
check("each segment carries its start and end",
      all("start" in s and "end" in s for s in _r["segments"]),
      str(_r["segments"][:1]))
check("the times match what whisper reported",
      _r["segments"][1]["start"] == 2.4 and _r["segments"][1]["end"] == 8.0,
      str(_r["segments"][1]))
check("a readable clock is stored alongside the seconds",
      _r["segments"][1]["at"] == "00:02", _r["segments"][1]["at"])
check("whitespace is trimmed off each segment",
      _r["segments"][0]["text"] == "Hello there.",
      repr(_r["segments"][0]["text"]))

# An empty segment is a whisper artefact, not speech. Keeping it would put blank
# rows in a transcript and inflate the count in a listing.
_r2 = _collect_segments([_Seg(0, 1, "  "), _Seg(1, 2, "real")], _Info())
check("blank segments are dropped", len(_r2["segments"]) == 1,
      f"{len(_r2['segments'])} kept")

# A segment without usable times must not raise; it is a malformed model output,
# not a reason to lose the whole transcript.
class _Bad:
    text = "no times"
_r3 = _collect_segments([_Bad()], _Info())
check("a segment with no times still transcribes",
      _r3["success"] and _r3["segments"][0]["start"] == 0.0,
      str(_r3.get("segments")))

check("no segments is success with empty text, not an error",
      _collect_segments([], None)["success"] is True)
check("an hour is formatted as H:MM:SS", _clock(3661) == "1:01:01", _clock(3661))
check("under an hour is MM:SS", _clock(65) == "01:05", _clock(65))
check("zero and junk both land on 00:00",
      _clock(0) == "00:00" and _clock("nonsense") == "00:00",
      f"{_clock(0)} / {_clock('nonsense')}")

# The contract the existing callers depend on. `ws_server` and the
# `transcribe_audio` skill read these three keys and nothing else.
check("the keys the existing callers read are all present",
      {"success", "text", "language"} <= set(_r),
      str(sorted(_r)))

print("\nThe meeting store")

from backend.meetings import store  # noqa: E402

_m = store.create("Roadmap sync", source="file")
check("a meeting gets an id", bool(_m.get("id")), str(_m.get("id")))
check("the id sorts chronologically",
      _m["id"][:10].count("-") == 2 and _m["id"][:4].isdigit(), _m["id"])
check("the title is kept", _m["title"] == "Roadmap sync", _m["title"])
check("a meeting starts with no transcript and no summary",
      not _m["transcript"] and not _m["summary"])

store.set_transcript(_m["id"], "Alice: hello. Bob: hi.",
                     segments=_r["segments"], language="en")
_g = store.get(_m["id"])
check("the transcript is stored", _g["transcript"] == "Alice: hello. Bob: hi.",
      repr(_g["transcript"]))
check("the segments are stored with it",
      len(_g["segments"]) == 3, str(len(_g["segments"])))
check("the language is stored", _g["language"] == "en", _g["language"])
check("finishing sets an end time", _g["ended_at"] is not None)

store.set_summary(_m["id"], "Decided to ship.",
                  decisions=["Ship it"],
                  actions=[{"who": "Bob", "what": "fix churn"}])
_g2 = store.get(_m["id"])
check("the summary is stored", _g2["summary"] == "Decided to ship.",
      _g2["summary"])
check("decisions are stored", _g2["decisions"] == ["Ship it"],
      str(_g2["decisions"]))
check("actions are stored", _g2["actions"][0]["who"] == "Bob",
      str(_g2["actions"]))
check("summarising records when it happened",
      _g2["summarised_at"] is not None)

# A second pass that finds no decisions must not erase the first pass's. This is
# the difference between "no new decisions" and "there were none".
store.set_summary(_m["id"], "Second pass, nothing new.", decisions=None)
_g3 = store.get(_m["id"])
check("a summary with no decisions keeps the earlier ones",
      _g3["decisions"] == ["Ship it"], str(_g3["decisions"]))
check("but the summary itself is replaced",
      _g3["summary"] == "Second pass, nothing new.", _g3["summary"])

_lst = store.list_meetings()
check("the listing finds the meeting", len(_lst) == 1, str(len(_lst)))
check("the listing drops the transcript",
      "transcript" not in _lst[0], str(sorted(_lst[0])))
check("the listing reports how many segments there were",
      _lst[0].get("segment_count") == 3, str(_lst[0].get("segment_count")))

_st = store.stats()
check("stats count the meeting", _st["count"] == 1, str(_st))
check("stats count it as summarised", _st["summarised"] == 1, str(_st))
check("stats count it as having actions", _st["with_actions"] == 1, str(_st))

check("a second meeting gets a distinct id",
      store.create("Roadmap sync")["id"] != _m["id"])

print("\nAn id cannot reach outside the store")

# Two layers: a character filter that rejects anything odd, and a resolved-path
# containment check behind it. Revert-verified honestly — removing the
# containment check alone does NOT fail this suite, because the filter already
# refuses every id below. Removing the FILTER does fail it. So the pair is
# verified, and the containment check is defence in depth rather than the layer
# doing the work. Said plainly rather than implying both were exercised.

# The id arrives from a tool argument. Character filtering alone is not a
# containment guarantee, so this asserts the resolved path stays inside - which
# is the property that matters and does not depend on the filter being complete.
_root = store.MEETINGS_DIR.resolve()
for _bad in ["../../settings", "a/b", "a\\b", "%2e%2e", ""]:
    try:
        store._path(_bad)
        check(f"refuses the id {_bad!r}", False, "was accepted")
    except ValueError:
        check(f"refuses the id {_bad!r}", True)

for _odd in ["..", "...", "con"]:
    try:
        _p = store._path(_odd)
        _inside = True
        try:
            _p.relative_to(_root)
        except ValueError:
            _inside = False
        check(f"{_odd!r} stays inside the meetings directory", _inside, str(_p))
    except ValueError:
        check(f"{_odd!r} is refused outright", True)

check("an unknown id reads as missing, not as an error",
      store.get("2020-01-01-0000-nope") is None)
check("an unusable id reads as missing too",
      store.get("../../etc/passwd") is None)

print("\nThe dashboard surface is wired up")

# The page talks to the backend over these method names. A rename on one side
# and not the other is a broken page that still passes every other check, so the
# two are asserted to agree.
import re as _re  # noqa: E402
_ws = open(os.path.join(ROOT, "backend", "ws_server.py"), encoding="utf-8").read()
_page = open(os.path.join(ROOT, "dashboard", "src", "app", "meetings", "page.tsx"),
             encoding="utf-8").read()
for _method in ["meetings.list", "meetings.get", "meetings.transcribe",
                "meetings.summarise", "meetings.delete", "meetings.rename"]:
    # Named `_method`, not `_m`: an earlier version reused `_m`, which is the
    # meeting dict later in this file, and broke the delete checks below it.
    check(f"the backend registers {_method}",
          f'_server.register("{_method}"' in _ws, "not registered")
    check(f"the page calls {_method}",
          f"'{_method}'" in _page, "the page never calls it")

check("the Meetings page is in the nav",
      "/meetings" in open(os.path.join(ROOT, "dashboard", "src", "app",
                                       "layout.tsx"), encoding="utf-8").read(),
      "the page would exist but be unreachable")

print("\nDeleting and pruning")

check("delete removes the meeting", store.delete(_m["id"]) is True)
check("and it is really gone", store.get(_m["id"]) is None)
check("deleting something absent is False, not an exception",
      store.delete("2020-01-01-0000-nope") is False)

import shutil  # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)

print()
if fails:
    print(f"{len(fails)} FAILED")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All meeting checks passed.")
