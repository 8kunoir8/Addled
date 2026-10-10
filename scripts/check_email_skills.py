"""Checks for the integration skills: email and local transcription.

Two things are being guarded here, and both would be dangerous to get wrong:

1. **Sending mail must be gated.** A skill that sends email without approval is
   a skill that can mail the wrong person, and mail cannot be recalled. The
   gate is the whole reason this is safe to expose at all, so it is asserted
   directly rather than assumed from the flag being present in the source.

2. **"No account" must not read as "no mail".** An empty inbox and an
   unconfigured account are very different answers. An agent told "no mail" on
   an account that was never set up will report a false all-clear, which is
   worse than an error, because it sounds like good news.

Transcription is checked for the cheap failure modes (no path, missing file)
rather than by shipping an audio fixture.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_email_skills.py
"""

from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.skills.registry import skill_registry  # noqa: E402

fails: list[str] = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def _skills() -> dict:
    return {s.name: s for s in skill_registry.list_all()}


skills = _skills()

# -- the skills exist at all ------------------------------------------------
for name in ("email_list", "email_search", "email_send", "transcribe_audio"):
    check(f"{name} is registered", name in skills,
          "not present in the registry")

# -- sending is gated; reading is not --------------------------------------
# Reading mail is harmless and prompting for it would train the user to click
# through approvals. Sending is irreversible. The two must differ.
if "email_send" in skills:
    check("email_send requires approval",
          bool(skills["email_send"].requires_approval),
          "email could be sent with no confirmation")
if "email_list" in skills:
    check("email_list does not require approval",
          not skills["email_list"].requires_approval,
          "reading mail would nag for approval")
if "email_search" in skills:
    check("email_search does not require approval",
          not skills["email_search"].requires_approval,
          "searching mail would nag for approval")

# -- input validation, so the model gets words not an SMTP error -----------
sent = []
if "email_send" in skills:
    async def _call(params):
        return await skills["email_send"].handler(params)

    r = asyncio.run(_call({}))
    check("email_send refuses without a recipient",
          not r.get("success") and r.get("error"), f"got {r}")
    r = asyncio.run(_call({"to": "not-an-address", "body": "hi"}))
    check("email_send rejects something that is not an address",
          not r.get("success") and "look like" in str(r.get("error", "")),
          f"got {r}")
    r = asyncio.run(_call({"to": "a@b.com"}))
    check("email_send refuses an empty message",
          not r.get("success") and r.get("error"), f"got {r}")

if "email_search" in skills:
    r = asyncio.run(skills["email_search"].handler({}))
    check("email_search asks for a query rather than searching for nothing",
          not r.get("success") and r.get("error"), f"got {r}")

# -- "not configured" is distinguishable from "nothing there" --------------
#
# This machine has no mail account, so both calls take the not-configured
# branch. The point is that they SAY so, instead of returning an empty list
# that reads as a clean inbox.
for name, params in (("email_list", {}), ("email_search", {"query": "x"})):
    if name not in skills:
        continue
    r = asyncio.run(skills[name].handler(params))
    check(f"{name} says the account is unconfigured rather than reporting no mail",
          (r.get("error") and "not set up" in str(r["error"]).lower()),
          f"an unconfigured account answered {r}")

# -- transcription fails usefully -----------------------------------------
if "transcribe_audio" in skills:
    r = asyncio.run(skills["transcribe_audio"].handler({}))
    check("transcribe_audio asks for a path",
          not r.get("success") and r.get("error"), f"got {r}")
    r = asyncio.run(skills["transcribe_audio"].handler({"path": "no-such.wav"}))
    check("transcribe_audio reports a missing file as missing",
          not r.get("success") and "no such file" in str(r.get("error", "")).lower(),
          f"got {r}")

# -- the roles that should hold them, do -----------------------------------
from backend.swarm.roster import DEFAULT_TYPES  # noqa: E402

have = set(skills)
for agent_type, spec in DEFAULT_TYPES.items():
    for name in spec.get("skills", []):
        if name.startswith("guidelines:"):
            continue
        check(f"{agent_type} refers to a skill that exists ({name})",
              name in have, "the roster names a skill that is not registered")

# The general desk is the fallback for anything unrouted, so it is the one that
# most needs mail; the writer produces documents and sends them.
check("the general desk can reach email",
      "email_send" in DEFAULT_TYPES["general"]["skills"],
      "an unrouted task would have no way to send mail")
check("the general desk can transcribe a recording",
      "transcribe_audio" in DEFAULT_TYPES["general"]["skills"],
      "an unrouted recording could not be transcribed")
# The writer must be able to PRODUCE a document and DELIVER it. The old
# assertion named `doc-coauthoring` and `internal-comms`, neither of which
# exists anywhere -- so it required two dead names in the roster to pass, and
# the roster obliged. Tested here against skills that are real.
_writer = set(DEFAULT_TYPES["writer"]["skills"])
check("the writer desk can produce a document",
      "word_create" in _writer,
      "the writer has no way to produce a document")
check("the writer desk can deliver what it writes",
      bool({"email_send", "send_message"} & _writer),
      "the writer cannot send what it produces")

if fails:
    print("FAILURES:")
    for f in fails:
        print("  -", f)
    print(f"\n{len(fails)} failure(s)")
    sys.exit(1)

print("PASS: integration skills -- mail is readable without a prompt and "
      "un-sendable without approval, an unconfigured account says so instead "
      "of reporting an empty inbox, and transcription fails usefully")
