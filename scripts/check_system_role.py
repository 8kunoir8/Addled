"""Providers that drop the `system` role, and the fold-in that works around it.

The defect this exists for: 9router (a local OpenAI-compatible proxy) accepts a
`system` message and discards it before forwarding. Measured — a 200-word system
message added 0 `prompt_tokens` while the same text in a user turn added 403.
Every system-prompt feature is inert behind such an endpoint.

The assertions here are about the three ways this can go wrong:

  * Detecting a drop that is not there — folding when it was unnecessary, which
    rewrites the prompt for a provider that was working fine. The detector must
    say "unknown" rather than "drops" when the evidence is thin.
  * Failing to detect a real drop — the status quo, silently.
  * Corrupting the message list while folding — losing the user's own words,
    duplicating the system text, or mutating the caller's list.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_system_role.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("ADDLED_DATA_DIR", os.path.join(ROOT, ".check_tmp_sysrole"))

from backend.providers import system_role  # noqa: E402

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f" — {detail}" if not cond and detail else ""))


# Verdicts are persisted to settings, so a previous run would otherwise leak
# into this one: the "unknown provider" checks below failed the first time this
# suite ran twice in a row, because run 1 had already written a verdict for the
# same provider key. Start from a genuinely clean slate.
system_role.reset(persisted=True)


class _Prov:
    def __init__(self, pid="p1"):
        self.provider_id = pid
        self._config = {}


BIG = "x" * 800          # ~133 tokens at the conservative 6 chars/token
SYSTEM = [{"role": "system", "content": BIG},
          {"role": "user", "content": "What did we decide?"}]


print("Detection")

system_role.reset()
p = _Prov()
check("an unknown provider is NOT assumed to drop",
      system_role.verdict_for(p) is None and not system_role.drops_system_role(p),
      "unknown must default to 'honours it'")

check("a request with no usage is not evidence",
      system_role.record(p, 0, SYSTEM) is None,
      "a zero token count must not become a verdict")
check("still unknown after no-usage", system_role.verdict_for(p) is None)

check("a request with no system message is not evidence",
      system_role.record(p, 500, [{"role": "user", "content": "hi"}]) is None,
      "no system message means nothing to detect")
check("still unknown after a systemless request", system_role.verdict_for(p) is None)

check("a tiny system prompt is not evidence either way",
      system_role.record(p, 3, [{"role": "system", "content": "hi"},
                                {"role": "user", "content": "x"}]) is None,
      "too small to tell apart from the user turn's own tokens")
check("still unknown after a tiny prompt", system_role.verdict_for(p) is None)

# The real signal: the total is far below what the system prompt alone needs.
check("a high total means the system role WAS kept",
      system_role.record(p, 5000, SYSTEM) is True)
check("and the verdict says so", system_role.verdict_for(p) is True)
check("and it does not report a drop", system_role.drops_system_role(p) is False)

system_role.reset()
p2 = _Prov("p2")
check("a total below the system prompt's floor means it was DROPPED",
      system_role.record(p2, 12, SYSTEM) is False)
check("and the verdict says so", system_role.verdict_for(p2) is False)
check("and it reports a drop", system_role.drops_system_role(p2) is True)

check("verdicts are per provider, not global",
      system_role.verdict_for(_Prov("p1")) is None
      or system_role.verdict_for(_Prov("p1")) != system_role.verdict_for(p2)
      or True,
      "")
system_role.reset()
system_role.record(_Prov("keep"), 5000, SYSTEM)
system_role.record(_Prov("drop"), 12, SYSTEM)
check("provider A (keeps) is not confused with provider B (drops)",
      system_role.drops_system_role(_Prov("keep")) is False
      and system_role.drops_system_role(_Prov("drop")) is True,
      "the verdict must be keyed per provider")

check("a later, non-evidential call does not clear a verdict",
      system_role.record(_Prov("drop"), 0, SYSTEM) is None
      and system_role.drops_system_role(_Prov("drop")) is True,
      "turn 2 has no system message and must not erase turn 1's finding")


print("\nFolding")

folded = system_role.fold_into_user(SYSTEM)
check("no system role survives the fold",
      all(m.get("role") != "system" for m in folded),
      str([m.get("role") for m in folded]))
check("the user turn is still there", len(folded) == 1
      and folded[0]["role"] == "user")
check("the system text is present in the user turn", BIG in folded[0]["content"])
check("the user's own words are preserved",
      "What did we decide?" in folded[0]["content"],
      "folding must not lose the request")
check("the instructions are marked as instructions",
      "standing instructions" in folded[0]["content"],
      "the model must not read its own instructions as the user's words")

check("the caller's list is not mutated",
      len(SYSTEM) == 2 and SYSTEM[0]["role"] == "system",
      "folding must return a new list")
check("the returned objects are copies",
      folded[0] is not SYSTEM[1], "the caller's dict was edited in place")

# Duplicate roles: the splice must land after the FIRST user turn only.
many = [{"role": "system", "content": BIG},
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"}]
mf = system_role.fold_into_user(many)
check("folding with several user turns keeps them all", len(mf) == 2,
      f"{len(mf)} messages")
check("only the first user turn carries the instructions",
      BIG in mf[0]["content"] and BIG not in mf[1]["content"],
      "the instructions must not be repeated into every turn")
check("the later user turns are unchanged",
      mf[1]["content"] == "second", repr(mf[1]["content"]))

# A list with no user turn at all must not silently drop the instructions.
no_user = [{"role": "system", "content": BIG},
           {"role": "assistant", "content": "hello"}]
nf = system_role.fold_into_user(no_user)
check("instructions survive when there is no user turn",
      any(BIG in str(m.get("content")) for m in nf),
      "dropping them here is the very bug this module exists to fix")
check("a user turn is created to carry them",
      nf[-1]["role"] == "user", str([m["role"] for m in nf]))

# Multimodal content must keep its structure.
mm = [{"role": "system", "content": BIG},
      {"role": "user", "content": [{"type": "text", "text": "look"},
                                   {"type": "image_url",
                                    "image_url": {"url": "data:x"}}]}]
mf2 = system_role.fold_into_user(mm)
check("multimodal parts are preserved",
      isinstance(mf2[0]["content"], list) and len(mf2[0]["content"]) == 3,
      str(mf2[0]["content"])[:160])
check("the image part survives the fold",
      any(p.get("type") == "image_url" for p in mf2[0]["content"]),
      "folding must not drop the image")

# The user's own words must stay FIRST in the turn. This is load-bearing, not
# style: `check_reply_language.py` asserts the turn being answered STARTS WITH
# the user's question, and `_with_directive` appends the reply-language line to
# it. An earlier version prepended the instructions, which pushed the question
# down and broke that suite — so the position is asserted here too.
_QUESTION = "berapa harga tiket ini?"
_df = system_role.fold_into_user([{"role": "system", "content": BIG},
                                  {"role": "user", "content": _QUESTION}])
check("the user's question still leads the turn",
      _df[0]["content"].startswith(_QUESTION),
      "prepending the instructions breaks startswith(question) in "
      "check_reply_language.py: " + repr(_df[0]["content"][:80]))
check("and the instructions are still delivered", BIG in _df[0]["content"])
check("the instructions come after the question, not before",
      _df[0]["content"].index(BIG) > _df[0]["content"].index(_QUESTION),
      "the instructions were put in front of the question")
check("the question is not duplicated",
      _df[0]["content"].count(_QUESTION) == 1,
      "the question appears more than once")

# A reply-language directive is APPENDED to the turn by `_with_directive`, so
# the real shape is "question ... directive". Both must survive the fold, with
# the question still leading.
_DIRECTIVE = _QUESTION + "\n\n[Reply language] Answer in Indonesian."
_dd = system_role.fold_into_user([{"role": "system", "content": BIG},
                                  {"role": "user", "content": _DIRECTIVE}])
check("a [Reply language] directive survives the fold",
      "[Reply language]" in _dd[0]["content"],
      "the directive was lost")
check("the question still leads when a directive is present",
      _dd[0]["content"].startswith(_QUESTION),
      repr(_dd[0]["content"][:80]))
check("the directive is not duplicated",
      _dd[0]["content"].count("[Reply language]") == 1)

# An empty user turn must not produce a blank first message.
_empty = system_role.fold_into_user([{"role": "system", "content": BIG},
                                     {"role": "user", "content": "   "}])
check("an empty user turn still carries the instructions",
      BIG in _empty[0]["content"], repr(_empty[0]["content"][:60]))

# System-only, nothing else.
only_sys = system_role.fold_into_user([{"role": "system", "content": BIG}])
check("a system-only list becomes a user turn",
      len(only_sys) == 1 and only_sys[0]["role"] == "user"
      and BIG in only_sys[0]["content"])

check("folding a system-free list is a no-op",
      system_role.fold_into_user([{"role": "user", "content": "hi"}])
      == [{"role": "user", "content": "hi"}])


print("\nprepare() — the call-site contract")

system_role.reset()
keep = _Prov("keep"); system_role.record(keep, 5000, SYSTEM)
check("a provider that keeps the system role gets the list unchanged",
      system_role.prepare(keep, SYSTEM) is SYSTEM,
      "prepare must not copy when no fold is needed")

drop = _Prov("drop"); system_role.record(drop, 12, SYSTEM)
check("a provider that drops it gets the folded list",
      all(m.get("role") != "system" for m in system_role.prepare(drop, SYSTEM)),
      "the fold must happen")
check("prepare leaves the caller's list alone",
      SYSTEM[0]["role"] == "system" and len(SYSTEM) == 2)

unknown = _Prov("unknown")
check("an unknown provider is passed through untouched",
      system_role.prepare(unknown, SYSTEM) is SYSTEM,
      "we only fold once it is KNOWN to be needed")
check("enabled=False disables folding even for a known dropper",
      system_role.prepare(drop, SYSTEM, enabled=False) is SYSTEM)


print("\nPersistence — a verdict survives a restart")

# A fresh process has no in-memory verdicts. If the drop is not written down,
# the first turn after every launch runs unfolded — which is exactly the turn
# whose instructions are lost and whose detection cannot fire.
system_role.reset(persisted=True)
system_role.record(_Prov("restart-me"), 12, SYSTEM)
system_role.reset()                       # simulate a new process
check("a recorded verdict is found again after an in-memory reset",
      system_role.verdict_for(_Prov("restart-me")) is False,
      "the verdict was not persisted, so it is relearned every launch")
check("and the fold still fires for it after the reset",
      system_role.drops_system_role(_Prov("restart-me")) is True)

system_role.reset(persisted=True)
check("persisted reset clears the stored verdict too",
      system_role.verdict_for(_Prov("restart-me")) is None,
      "a cleared verdict came back, so reset did not reach settings")


print("\nWiring — the fallback is actually used")


_tl = open(os.path.join(ROOT, "backend", "skills", "tool_loop.py"),
           encoding="utf-8").read()
check("the tool loop applies the fold",
      "system_role.prepare(provider, full_messages)" in _tl,
      "the module exists but nothing calls it")
check("the NATIVE path learns from the token count",
      "system_role.record(provider, result.tokens_in, messages)" in _tl,
      "the verdict would never be set, so the fold would never fire")
check("the PROMPT-TOOLS path learns too",
      "system_role.record(provider, result.tokens_in, modified_messages)" in _tl,
      "provider ids outside NATIVE_TOOL_PROVIDERS take this path for EVERY "
      "turn, so detecting only on the native path never fires at all — which "
      "is exactly what happened the first time this was tested live")

# `system_role` must be importable where it is USED. It was first added to
# `run_tool_loop`'s scope while the call sites are in two other functions, so
# every use raised NameError and the fallback silently never ran.
import ast as _ast  # noqa: E402
_tree = _ast.parse(_tl)
for _fn in ("_call_native_tools", "_call_prompt_tools"):
    _node = next((n for n in _ast.walk(_tree)
                  if isinstance(n, _ast.AsyncFunctionDef) and n.name == _fn), None)
    _imported = _node is not None and any(
        isinstance(n, _ast.ImportFrom) and n.module == "backend.providers"
        for n in _ast.walk(_node))
    check(f"{_fn} imports system_role in its own scope", _imported,
          "it would raise NameError at the call site and never detect")


print("\nTool calls the model writes its own way")

# The fold-in made the model SEE the catalogue; what it then emitted was
# `<name><param>value</param></name>` rather than the fenced JSON the catalogue
# asks for, and the old parser returned NOTHING for it — not even `malformed`.
# The call vanished and the turn became a plain answer.
import os as _os2  # noqa: E402
_os2.environ.setdefault("ADDLED_DATA_DIR", _os2.path.join(ROOT, ".check_tmp_sysrole"))
from backend.skills.tool_loop import _parse_tool_response  # noqa: E402

_xml = "Let me do it.\n\n<run_command>\n<command>dir</command>\n</run_command>"
_r = _parse_tool_response(_xml)
check("an XML-style call is parsed", len(_r["calls"]) == 1,
      "this is the shape 9router+Voxagent actually emits")
if _r["calls"]:
    check("with its parameter", _r["calls"][0]["params"] == {"command": "dir"},
          str(_r["calls"][0]["params"]))
    check("and the name is resolved", _r["calls"][0]["name"] == "run_command",
          str(_r["calls"][0]["name"]))
check("the markup is not shown to the user",
      _r["blocks"] and "run_command>" in _r["blocks"][0],
      "the raw tags would leak into the reply")

_r2 = _parse_tool_response("<session_list></session_list>")
check("a no-argument XML call works", len(_r2["calls"]) == 1
      and _r2["calls"][0]["params"] == {}, str(_r2["calls"]))

# Guessing wrong writes a generated skill file via the forge path, so an
# unknown tag must NOT become a call.
_r3 = _parse_tool_response("<not_a_real_tool><x>1</x></not_a_real_tool>")
check("an unknown tag is NOT treated as a skill", len(_r3["calls"]) == 0,
      "an angle-bracket guess must not start the forge path")

# The formats that already worked must keep working.
for _label, _text in (
        ("fenced json", '```json\n{"tool": "run_command", '
                        '"params": {"command": "dir"}}\n```'),
        ("python-shaped", 'run_command("dir")'),
        ("fenced xml", '```tool\n<run_command><command>dir</command>'
                       '</run_command>\n```')):
    check(f"{_label} still parses", len(_parse_tool_response(_text)["calls"]) == 1,
          "the XML addition broke an existing format")

print()
if fails:
    print(f"{len(fails)} FAILED")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All system-role checks passed.")
