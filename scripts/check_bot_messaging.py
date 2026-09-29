"""Can Addled send through a bot, read back what was exchanged, and deliver a
scheduled reminder?

Three capabilities, each of which was missing or half-built:

  * SENDING — every `sendMessage` call in the bridge was inside the incoming
    handler, so nothing could initiate a message. There was no `bots.send` RPC
    and no skill.
  * HISTORY — a bridge only ever saw the single incoming message.
  * SCHEDULED DELIVERY — `_run_notify` broadcast `bot.notify` and NOTHING
    consumed it: `onNotification` was defined in the shared client and called by
    zero bot files. The reminder reached the dashboard, the bubble and TTS, and
    never the phone.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_bot_messaging.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []

def check(label, ok, detail=""):
    if ok:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        fails.append(label)

def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()

# A scratch history store, so this cannot touch real conversations.
_TMP = tempfile.mkdtemp(prefix="bothist_")
from backend.config import config  # noqa: E402

config.set("bots", "history_dir", value=_TMP)

from backend.bots import manager as bots  # noqa: E402
from backend.memory import bot_history  # noqa: E402

print("The three new skills exist and are gated correctly")
from backend.skills.registry import skill_registry  # noqa: E402

for name, needs_approval in (("send_message", True),
                             ("bot_status", False),
                             ("chat_history", False)):
    skill = skill_registry.get(name)
    check(f"{name} is registered", skill is not None, "the model cannot call it")
    if skill:
        check(f"{name} approval={needs_approval}",
              bool(skill.requires_approval) == needs_approval,
              f"got {skill.requires_approval}")

send = skill_registry.get("send_message")
check("send_message takes an alias for the number",
      "to" in (send.aliases or {}) and "recipient" in send.aliases["to"],
      str(send.aliases))
check("send_message takes an alias for the text",
      "message" in (send.aliases or {}).get("text", ()), str(send.aliases))

print("\nSending refuses what it cannot do, and says why")
# NOTE: `send_message` is gated, so going through `execute()` raises the
# approval card BEFORE the handler ever runs — the refusal never gets a chance
# to be produced. That is correct (the user is asked, and the handler validates
# when the answer comes back), so the validation is exercised through the
# handler directly here, and the gating is asserted separately below.
_send_handler = skill_registry.get("send_message").handler

# The parameter alias/validation layer runs first and catches a missing field,
# which is a better error than the handler's own — it names what was expected.
_sk = skill_registry.get("send_message")
_, _trouble = _sk.normalise({"text": "hi"})
check("a missing destination is caught before the handler",
      bool(_trouble) and "to" in _trouble, _trouble)
_, _trouble2 = _sk.normalise({"to": "6281234567890"})
check("a missing message is caught before the handler",
      bool(_trouble2) and "text" in _trouble2, _trouble2)

bad = asyncio.run(_send_handler({"platform": "nope", "to": "6281234567890",
                                 "text": "hi"}))
check("an unknown platform is named",
      not bad.get("success") and "Unknown bot" in str(bad.get("error")),
      str(bad.get("error"))[:140])

stopped = asyncio.run(_send_handler({"platform": "whatsapp",
                                     "to": "6281234567890", "text": "hello"}))
check("a stopped bot is reported, not attempted",
      not stopped.get("success")
      and "not running" in str(stopped.get("error")).lower(),
      str(stopped.get("error"))[:180])

empty_text = asyncio.run(_send_handler({"platform": "whatsapp",
                                        "to": "6281234567890", "text": ""}))
check("an empty message is refused",
      not empty_text.get("success") and "say" in str(empty_text.get("error")).lower(),
      str(empty_text.get("error"))[:140])

no_to = asyncio.run(_send_handler({"platform": "whatsapp", "to": "",
                                   "text": "hi"}))
check("a missing destination is refused by the handler too",
      not no_to.get("success") and "phone number" in str(no_to.get("error")).lower(),
      str(no_to.get("error"))[:140])

check("the skill is gated, so a send needs approval",
      bool(skill_registry.get("send_message").requires_approval),
      "Addled could message a third party without asking")

print("\nThe manager validates before it broadcasts")
res = asyncio.run(bots.send("whatsapp", "6281234567890", "   "))
check("empty text refused", not res.get("success"), str(res))
res = asyncio.run(bots.send("whatsapp", "", "hello"))
check("empty destination refused", not res.get("success"), str(res))
res = asyncio.run(bots.send("nope", "6281234567890", "hello"))
check("unknown platform refused", not res.get("success"), str(res))

print("\nA send that is answered comes back with the answer")
# Simulate the bot answering, without a real process.
bots._procs["whatsapp"] = type("P", (), {"returncode": None})()

async def _drive():
    """Call send() while resolving its waiter the way the bot would."""
    task = asyncio.ensure_future(
        bots.send("whatsapp", "6281234567890", "hello from the test"))
    for _ in range(200):
        await asyncio.sleep(0.005)
        pending = list(bots._send_waiters)
        if pending:
            bots.resolve_send(pending[0], {"success": True, "to": "x"})
            break
    return await task

try:
    answered = asyncio.run(_drive())
    check("a resolving bot settles the call", answered.get("success") is True,
          str(answered))
except Exception as e:  # noqa: BLE001
    check("a resolving bot settles the call", False, f"{type(e).__name__}: {e}")

print("\nA send nobody answers times out with a useful message")
_saved_timeout = bots.SEND_TIMEOUT_S
bots.SEND_TIMEOUT_S = 0.15
try:
    timed_out = asyncio.run(
        bots.send("whatsapp", "6281234567890", "into the void"))
    check("a silent bot does not hang the turn", not timed_out.get("success"),
          str(timed_out))
    check("and the message names the bot and the way out",
          "did not answer" in str(timed_out.get("error"))
          and "log" in str(timed_out.get("error")).lower(),
          str(timed_out.get("error"))[:200])
finally:
    bots.SEND_TIMEOUT_S = _saved_timeout
    bots._procs.pop("whatsapp", None)

print("\nHistory records what a bridge exchanged")
# Cleared first: the send test above already recorded an outbound message to
# 6281234567890 (that is the point of it), and leaving it would put three turns
# in a store this section expects to hold two.
shutil.rmtree(_TMP, ignore_errors=True)
os.makedirs(_TMP, exist_ok=True)

bot_history.record_turn("whatsapp", "628111@s.whatsapp.net",
                        "what is the weather", "It is fine.")
turns = bot_history.recent(platform="whatsapp")
check("both sides of the turn are kept", len(turns) == 2, str(len(turns)))
check("the question is there",
      turns and turns[0].get("role") == "user"
      and "weather" in turns[0].get("text", ""), str(turns)[:160])
check("the reply is there",
      len(turns) > 1 and turns[1].get("role") == "assistant", str(turns)[:160])

check("the dashboard is NOT recorded as a bot chat",
      bot_history.record_turn("dashboard", "conv-1", "hi", "hello") is None
      and not any(c.get("conversation") == "conv-1"
                  for c in bot_history.conversations()),
      "the user's own chat page would double every turn")

print("\nA number asked for the way a person gives it still finds the chat")
loose = bot_history.recent(conversation="628111")
check("bare digits match a stored JID", len(loose) == 2, str(len(loose)))
check("a different number finds nothing",
      bot_history.recent(conversation="629999") == [], "")

print("\nOutbound messages are recorded, including failures")
bot_history.record_outbound("whatsapp", "628222@s.whatsapp.net",
                            "a reminder", True)
bot_history.record_outbound("whatsapp", "628333@s.whatsapp.net",
                            "did not go", False, "Not on WhatsApp")
out = bot_history.recent(conversation="628222")
check("a delivered send is recorded", len(out) == 1, str(out)[:120])
check("and marked outbound", out and out[0].get("outbound") is True, str(out)[:120])
failed = bot_history.recent(conversation="628333")
check("a failed send is recorded too", len(failed) == 1, str(failed)[:120])
check("with the reason",
      failed and failed[0].get("delivered") is False
      and "not on whatsapp" in str(failed[0].get("error", "")).lower(),
      str(failed)[:160])

print("\nHistory is bounded and survives a restart")
# Cleared first, for the same reason as above: this section asserts a cap on a
# conversation, so anything already in the store would be counted with it.
shutil.rmtree(_TMP, ignore_errors=True)
os.makedirs(_TMP, exist_ok=True)

for i in range(bot_history.MAX_PER_CONVERSATION + 40):
    bot_history.record_turn("telegram", "chat-1", f"q{i}", f"a{i}")
stored = [c for c in bot_history.conversations() if c["conversation"] == "chat-1"]
check(f"exactly one conversation is held",
      len(stored) == 1, str(stored)[:160])
check(f"and it is capped at {bot_history.MAX_PER_CONVERSATION}",
      stored and stored[0]["turns"] <= bot_history.MAX_PER_CONVERSATION,
      str(stored[0]["turns"]) if stored else "no conversation")
bounded = bot_history.recent(conversation="chat-1",
                             limit=bot_history.MAX_PER_CONVERSATION + 200)
check(f"reading back is capped too",
      len(bounded) <= bot_history.MAX_PER_CONVERSATION, str(len(bounded)))
check("the newest turn survived the trim",
      bounded and "a" + str(bot_history.MAX_PER_CONVERSATION + 39)
      in str(bounded[-1].get("text")), str(bounded[-1])[:120] if bounded else "")

on_disk = os.path.join(_TMP, "history.json")
check("the store was written", os.path.exists(on_disk), on_disk)
if os.path.exists(on_disk):
    try:
        parsed = json.loads(open(on_disk, encoding="utf-8").read())
        check("and it is valid JSON with conversations",
              isinstance(parsed, dict) and isinstance(
                  parsed.get("conversations"), list), str(parsed)[:120])
    except Exception as e:  # noqa: BLE001
        check("and it is valid JSON", False, f"{type(e).__name__}: {e}")
    # Re-read through the module, which loads from disk each call — this is the
    # survives-a-restart assertion, and it reads the conversation written above.
    reloaded = bot_history.recent(conversation="chat-1")
    check("re-reading from disk finds the chat", len(reloaded) == 20,
          f"got {len(reloaded)} (default limit is 20)")

print("\nA corrupt history file does not break the feature")
open(on_disk, "w", encoding="utf-8").write("{ not json")
check("recent() survives a corrupt file", bot_history.recent() == [],
      "it raised or returned garbage")
bot_history.record_turn("whatsapp", "628444@s.whatsapp.net", "after", "ok")
check("and recording recovers from it",
      len(bot_history.recent(conversation="628444")) == 2, "")

print("\nThe bot actually subscribes to what the backend broadcasts")
bot = read("bots/whatsapp-bot.js")
check("the bridge listens for bot.notify", "onNotification('bot.notify'" in bot,
      "the scheduler's broadcast would go nowhere — the original defect")
check("and for bots.send", "onNotification('bots.send'" in bot)
check("it reports the outcome", "bots.sendResult" in bot)
check("it respects the platform scoping",
      "platforms" in bot and "includes('whatsapp')" in bot,
      "every running bot would deliver the same reminder")
check("it remembers a chat so a reminder has a destination",
      "rememberChat" in bot and "last-chat.txt" in bot)
check("a phone number is normalised, not trusted raw",
      "normaliseJid" in bot and "@s.whatsapp.net" in bot)

print("\nThe backend broadcast carries the scoping field")
actions = read("backend/tasks/actions.py")
check("the reminder sends a platforms list",
      '"platforms": _notify_platforms()' in actions,
      "an unscoped broadcast is delivered once per running bot")
check("and the list is re-read, not cached",
      "def _notify_platforms" in actions,
      "a cached list keeps sending to a bot the user just turned off")

print("\nThe default config makes the feature work before anyone visits Settings")
# Read from DEFAULT_SETTINGS, not from the live config — this suite redirected
# history_dir to a scratch folder, so the live value is not the default.
from backend.config import DEFAULT_SETTINGS  # noqa: E402
check("notify_platforms defaults to empty (meaning any)",
      DEFAULT_SETTINGS["bots"].get("notify_platforms") == [],
      str(DEFAULT_SETTINGS["bots"].get("notify_platforms")))
check("history_dir defaults to empty (meaning the default location)",
      DEFAULT_SETTINGS["bots"].get("history_dir") == "",
      str(DEFAULT_SETTINGS["bots"].get("history_dir")))
check("the scratch redirect is what the live config has",
      config.get("bots", "history_dir", default=None) == _TMP,
      str(config.get("bots", "history_dir", default=None)))

shutil.rmtree(_TMP, ignore_errors=True)

print()
if fails:
    print(f"{len(fails)} FAILED")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("All bot messaging checks passed.")
