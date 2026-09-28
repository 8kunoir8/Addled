"""Checks for a bot being able to answer a permission request.

A gated tool asked for over Telegram announced itself to the dashboard and told
the phone to go and use it. The bot held the turn open and had no id to answer
with, because `chat.send` returned a *count* of tool results rather than the
requests themselves. This suite covers the pieces that make an answer possible,
and the two directions that must stay impossible:

* a chat must not be able to answer a request raised by a *different* chat, or
  by a different platform, or by a scheduled task — a "yes" typed in a group is
  not consent for a command someone else asked for; and
* a destructive name must not become permanently allowed by a bot clicking
  "Always allow", because the policy refuses those whoever asks.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_bot_approval.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.actions.executor import ActionRequest
from backend.approvals import pending, policy
import backend.ws_server as ws

fails: list[str] = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

def queue(executor, action_type, **params):
    """Put a request in the queue the way a chat turn does."""
    executor._approval_counter += 1
    aid = f"appr_{executor._approval_counter}"
    request = ActionRequest(action_type=action_type, params=params)
    executor._pending_approvals[aid] = request
    executor._note_origin(aid, request)
    return aid

def run():
    from backend.actions.executor import executor

    saved_policy = None
    try:
        from backend.config import config
        saved_policy = config.get("safety", "always_allow", default=None)
    except Exception:  # noqa: BLE001
        config = None

    try:
        # ---- the origin is recorded, and scoped --------------------------
        pending.clear()
        executor._pending_approvals.clear()

        aid = queue(executor, "write_file", path="a.txt",
                    source="telegram", conversation="chat-1")
        entry = pending.get(aid)
        check("an approval records its origin", entry is not None,
              "nothing was recorded")
        check("with the platform it came from",
              (entry or {}).get("source") == "telegram", str(entry))
        check("and the conversation it belongs to",
              (entry or {}).get("conversation") == "chat-1", str(entry))

        check("a request is found by its own conversation",
              [e["approval_id"] for e in
               pending.for_conversation("telegram", "chat-1")] == [aid],
              "its own chat could not find it")
        check("a different chat on the same platform cannot see it",
              pending.for_conversation("telegram", "chat-2") == [],
              "another chat saw it")
        check("the same chat id on another platform cannot see it",
              pending.for_conversation("whatsapp", "chat-1") == [],
              "another platform saw it")

        # ---- the conversation resolver only returns live requests --------
        found = asyncio.run(
            executor.resolve_by_conversation("telegram", "chat-1"))
        check("the conversation resolver finds a queued request",
              (found or {}).get("approval_id") == aid, str(found))
        check("and finds nothing for a conversation that asked nothing",
              asyncio.run(
                  executor.resolve_by_conversation("telegram", "chat-9"))
              is None, "an unknown chat resolved to something")

        # A recorded origin whose request is gone must not resolve: it would
        # answer into nothing and report success.
        executor._pending_approvals.pop(aid, None)
        check("a stale origin does not resolve to a request",
              asyncio.run(
                  executor.resolve_by_conversation("telegram", "chat-1"))
              is None, "a stale record still resolved")
        pending.forget(aid)

        # ---- answering by id runs it, and forgets the origin -------------
        pending.clear()
        executor._pending_approvals.clear()
        aid2 = queue(executor, "write_file", path="b.txt",
                     source="discord", conversation="chan-7")
        result = executor.deny(aid2)
        check("denying by id succeeds", result.success, str(result.error))
        check("and the origin is forgotten", pending.get(aid2) is None,
              "the origin outlived the answer")
        check("and the queue is clear", aid2 not in executor._pending_approvals,
              "it is still queued")

        # ---- a bot cannot make a destructive name permanent --------------
        # The button is only offered when the policy would accept the name; the
        # policy is what actually refuses, and it does so whoever calls.
        for guarded in ("delete_file", "run_command"):
            r = policy.always_allow(policy.SKILL, guarded)
            check(f"a bot cannot grant '{guarded}' permanently",
                  r.get("success") is False and r.get("protected") is True,
                  str(r))

        r = policy.always_allow(policy.SKILL, "send_email")
        check("an ordinary skill can still be granted", r.get("success") is True,
              str(r))

        # ---- the origin survives the tool call ---------------------------
        # A tool reaches the gate carrying only its own arguments, so the chat
        # that asked cannot be read off them. It travels in a ContextVar set
        # for the turn — and without this the request queued with NO origin,
        # which made it unanswerable from the very chat that asked for it.
        from backend import chat_context

        pending.clear()
        executor._pending_approvals.clear()
        token = None
        try:
            chat_context.set_origin("telegram", "chat-77")
            # Queued with params that name nothing about the conversation, the
            # way a real tool call arrives.
            aid4 = queue(executor, "write_file", path="d.txt")
            entry4 = pending.get(aid4) or {}
            check("a tool call with no conversation params still records the "
                  "chat that asked",
                  entry4.get("conversation") == "chat-77", str(entry4))
            check("and the platform too",
                  entry4.get("source") == "telegram", str(entry4))
            check("so that chat can answer it",
                  [e["approval_id"] for e in
                   pending.for_conversation("telegram", "chat-77")] == [aid4],
                  "the chat that asked could not answer")

            # An explicit param still wins over the ambient one.
            chat_context.set_origin("telegram", "chat-77")
            aid5 = queue(executor, "write_file", path="e.txt",
                         source="whatsapp", conversation="other")
            entry5 = pending.get(aid5) or {}
            check("params naming a conversation override the turn's",
                  entry5.get("conversation") == "other"
                  and entry5.get("source") == "whatsapp", str(entry5))

            # With nothing set, there is no origin — not a guessed one.
            pending.clear()
            executor._pending_approvals.clear()
            token = chat_context._turn.set({})
            aid6 = queue(executor, "write_file", path="f.txt")
            check("a turn with no origin records none",
                  (pending.get(aid6) or {}).get("conversation") == "",
                  str(pending.get(aid6)))
            check("and is therefore not answerable from any chat",
                  pending.for_conversation("telegram", "chat-77") == [],
                  "it was answerable without an origin")
        finally:
            if token is not None:
                chat_context._turn.reset(token)
            chat_context._turn.set({})

        # ---- the reply hands a bot ---------------------------------------
        # `_pending_for` is what becomes `pendingApprovals` in the chat reply.
        pending.clear()
        executor._pending_approvals.clear()
        aid3 = queue(executor, "write_file", path="c.txt",
                     source="telegram", conversation="chat-3")

        listed = ws._pending_for({"source": "telegram", "conversation": "chat-3"})
        check("the reply lists this chat's pending approval", len(listed) == 1,
              str(listed))
        first = listed[0] if listed else {}
        check("with the id a button needs",
              first.get("approval_id") == aid3, str(first))
        check("and enough to describe it",
              all(k in first for k in
                  ("kind", "name", "grantable", "command")), str(first))

        check("a reply with no conversation lists nothing, rather than "
              "everything on the machine",
              ws._pending_for({"source": "telegram"}) == [],
              "listed requests without being told which chat asked")
        check("another chat's reply lists nothing",
              ws._pending_for({"source": "telegram",
                               "conversation": "chat-4"}) == [],
              "it was handed another chat's request")

        # ---- a convenience request must not travel in two replies --------
        check("the same chat asking twice lists it once",
              len(ws._pending_for({"source": "telegram",
                                   "conversation": "chat-3"})) == 1,
              "duplicated")

        # ---- the bridges actually build the answer ------------------------
        # Read as text: these are Node files the Python suite cannot import.
        telegram = _read("bots/telegram-bot.js")
        discord = _read("bots/discord-bot.js")
        whatsapp = _read("bots/whatsapp-bot.js")

        check("Telegram renders an approval keyboard",
              "inline_keyboard" in telegram and "approvalKeyboard" in telegram,
              "no keyboard is built")
        check("Telegram handles the button press",
              "callback_query:data" in telegram, "no callback handler")
        check("Telegram sends all three verbs",
              all(v in telegram for v in ("action.approve", "action.deny",
                                          "approvals.alwaysAllow")),
              "a button would do nothing")
        check("Telegram declares its conversation",
              "conversation: String(sent.chat.id)" in telegram,
              "the approval could not be tied to the chat")

        check("Discord handles a button interaction",
              "isButton()" in discord, "buttons would be ignored")
        check("Discord builds a button row",
              "ActionRowBuilder" in discord and "approvalRow" in discord,
              "no components are built")
        check("Discord sends all three verbs",
              all(v in discord for v in ("action.approve", "action.deny",
                                         "approvals.alwaysAllow")),
              "a button would do nothing")
        check("Discord declares its conversation",
              "conversation: String(interaction.channelId" in discord
              or "conversation: String(msg.channelId" in discord,
              "the approval could not be tied to the channel")

        check("WhatsApp reads a typed answer",
              "classifyReply" in whatsapp, "no reply classifier")
        check("WhatsApp remembers what it asked",
              "waiting.set(sender" in whatsapp,
              "a reply could not find its request")
        check("WhatsApp sends all three verbs",
              all(v in whatsapp for v in ("action.approve", "action.deny",
                                          "approvals.alwaysAllow")),
              "an answer would do nothing")

        # ---- the word has to be unambiguous ------------------------------
        # "yes but also delete the other one" is not consent, and a stray word
        # must not be read as one. Evaluated by Node against the bot's own
        # helper, so this tests the real logic rather than a Python copy.
        for word, expected in (("yes", "allow"), ("ALWAYS", "always"),
                               ("no", "deny"), ("maybe later", None),
                               ("yes but also delete everything", None),
                               ("yes.", "allow")):
            verdict = _classify_with_node(word)
            check(f"the reply {word!r} is read as {expected!r}",
                  verdict == expected, f"got {verdict!r}")

        # ---- destructive requests do not offer "Always allow" -------------
        from backend.actions import approval_notice
        check("a destructive action is not announced as grantable",
              approval_notice._grantable("delete_file") is False,
              "the button would be offered")
    finally:
        pending.clear()
        executor._pending_approvals.clear()
        policy.clear()
        if config is not None and saved_policy is not None:
            config.set("safety", "always_allow", value=saved_policy)

def _read(rel: str) -> str:
    try:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""

def _classify_with_node(word: str):
    """Run the bot's own `classifyReply` against one input.

    Shelled out to Node because the logic is JavaScript. Re-implementing it in
    Python would test the copy rather than the thing that actually runs — which
    is how a check comes to pass while the bot is broken.

    Returns the verdict, or None when there is no Node to ask.
    """
    import shutil
    import subprocess

    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        return None
    source = _read("bots/whatsapp-bot.js")
    start = source.find("const YES")
    end = source.find("function approvalPrompt")
    if start < 0 or end <= start:
        return None
    script = (source[start:end]
              + f"\nprocess.stdout.write(String(classifyReply("
                f"{json.dumps(word)})));")
    try:
        proc = subprocess.run([node, "-e", script], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=30)
    except Exception:  # noqa: BLE001
        return None
    out = (proc.stdout or "").strip()
    if not out or out == "null":
        return None
    return out

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: bot approval — a chat can answer the request it raised, and "
          "only that one")
    return 0

if __name__ == "__main__":
    sys.exit(main())
