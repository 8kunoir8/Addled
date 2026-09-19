"""What the model is given must equal what the chat page shows.

Installed 1.0.18 answered "yoo apakabar ?" with the user's screen resolution. The
cause was not memory: `current_conversation` had survived every restart since
2026-08-16, so the pipeline attached the last twenty messages of that month-old
thread — two of which were screen-resolution exchanges — to a bare greeting. The
dashboard showed a blank chat, because on mount it reads only its own cache, so
the reply looked like it came from nowhere.

Three things made that possible, and each is asserted here because each is
invisible from the UI:

  1. **the conversation had no session boundary** — a new run carried on the old
     thread. A run must start a conversation of its own;
  2. **the rolling summary was not scoped to its conversation** — the file stores
     a `conversation_id` that the reader never checked, so a month-old
     mid-session compaction was announced as "[Earlier in this conversation]";
  3. **facts, timeline days and session summaries were injected unconditionally**
     — and `user_profile.auto_learn()` copied every fact into `preferences`, an
     always-on block, so an unrelated fact survived the gate anyway.

The positive halves matter as much as the negative ones: a gate that drops
everything would pass every "must not contain" assertion while making the agent
forget the user. Each unrelated check is paired with a related query that must
still find its memory.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_chat_scope.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


# Distinctive text, so "is it in the prompt?" cannot be answered by something the
# machine already had stored for real.
STALE_FACT = ("The user is actively trying to determine their screen resolution "
              "using a tool. ZZMARK_STALE_SCREEN.")
SCHED_FACT = ("The user is interested in debugging scheduler behavior on "
              "startup. ZZMARK_SCHED.")
OLD_CHAT = "ZZMARK_OLDCHAT tell me my screen size"
OLD_REPLY = "ZZMARK_OLDCHAT 1080x1920"

UNRELATED = "what is the capital of France"
FILLER = "yoo apakabar ?"
RELATED = "debug why the scheduler fires immediately on startup"


class _Result:
    def __init__(self, text):
        self.ok = True
        self.response = text
        self.tokens_in = 1
        self.tokens_out = 1
        self.tool_calls = []
        self.reasoning_content = ""
        self.error = ""


class FakeProvider:
    """Replies from a script and keeps what the pipeline handed it."""

    supports_vision = False

    def __init__(self, replies, provider_id="mock"):
        self.provider_id = provider_id
        self.replies = list(replies)
        self.payloads = []
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.payloads.append(messages)
        self.calls.append(kwargs)
        return _Result(self.replies.pop(0) if self.replies else "")


def prompt_text(payload) -> str:
    """Everything the provider was given, as one string."""
    out = []
    for m in payload:
        content = m.get("content")
        out.append(content if isinstance(content, str) else json.dumps(content))
    return "\n".join(out)


async def chat_once(ws_server, provider, message):
    """Run one turn through the real pipeline and return the full prompt."""
    provider.payloads.clear()
    await ws_server.run_chat_pipeline(message, record=False, announce=False,
                                     provider=provider)
    return prompt_text(provider.payloads[0])


async def main() -> int:
    import backend.config as config_mod
    import backend.memory.chat_history as ch
    import backend.memory.compaction as compaction
    import backend.memory.facts as facts
    import backend.memory.journal as journal
    import backend.memory.relevance as relevance
    import backend.memory.session_summary as session_summary
    import backend.memory.user_profile as user_profile
    import backend.memory.vector_store as vs
    import backend.ws_server as ws_server

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)

        # ---- point every store at the sandbox ----------------------------
        original = {
            "history": ch.HISTORY_PATH,
            "facts": facts.FACTS_PATH,
            "journal": journal.JOURNAL_DIR,
            "summaries": session_summary.SUMMARIES_PATH,
            "rolling": compaction.ROLLING_PATH,
            "profile": user_profile.PROFILE_PATH,
            "vectors": vs.DB_PATH,
            "settings": config_mod.SETTINGS_PATH,
        }
        ch.HISTORY_PATH = tmp_path / "chat_history.json"
        facts.FACTS_PATH = tmp_path / "facts.json"
        journal.JOURNAL_DIR = tmp_path / "journal"
        session_summary.SUMMARIES_PATH = tmp_path / "session_summaries.json"
        compaction.ROLLING_PATH = tmp_path / "rolling_summary.json"
        user_profile.PROFILE_PATH = tmp_path / "user_profile.json"
        # The store opens its connection in __init__, so point the module at a
        # sandbox file *and* replace the singleton; recall imports the name at
        # call time, so the swap is what takes effect.
        vs.DB_PATH = tmp_path / "vectors.db"
        vs.vector_store = vs.VectorStore()
        config_mod.SETTINGS_PATH = tmp_path / "settings.json"

        def reset_history(conversation_id=None, messages=None):
            ch.HISTORY_PATH.write_text(json.dumps({
                "conversations": {
                    "conv_old": {"id": "conv_old", "created": 1.0, "updated": 2.0,
                                 "title": "old", "messages": messages or []},
                },
                "current_conversation": conversation_id,
            }), encoding="utf-8")
            ch.chat_history._loaded = False
            ch.chat_history._data = {"conversations": {}, "current_conversation": None}

        # A month-old thread, exactly like the one this machine had.
        reset_history("conv_old", [
            {"role": "user", "content": OLD_CHAT, "timestamp": 1.0},
            {"role": "assistant", "content": OLD_REPLY, "timestamp": 2.0},
        ])
        facts.FACTS_PATH.write_text(json.dumps([
            {"id": 1, "text": STALE_FACT, "ts": 1.0, "source": "agent"},
            {"id": 2, "text": SCHED_FACT, "ts": 2.0, "source": "agent"},
        ]), encoding="utf-8")
        journal.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        session_summary.SUMMARIES_PATH.write_text(json.dumps([
            {"ts": 1.0, "summary": "Recent topics: ZZMARK_OLDCHAT duplicating anime "
                                   "folders | steam on linux"},
        ]), encoding="utf-8")
        compaction.ROLLING_PATH.write_text(json.dumps({
            "conversation_id": "conv_old",
            "entries": [{"ts": 1.0, "summary": "Earlier topics: ZZMARK_OLDCONV "
                                               "anime duplicates"}],
            "summarized": 40,
        }), encoding="utf-8")

        # ---- 1. a run starts its own conversation ------------------------
        before = ch.chat_history.get_context(max_messages=20)
        check("a stale thread is what the old build saw",
              any(OLD_CHAT in m["content"] for m in before),
              f"fixture not in place: {before}")

        previous = ch.chat_history.start_session()
        check("start_session reports the conversation it closed",
              previous == "conv_old", str(previous))
        check("and the pipeline now sees no history at all",
              ch.chat_history.get_context(max_messages=20) == [],
              str(ch.chat_history.get_context(max_messages=20)))

        ch.chat_history.add_message("user", "a fresh first message")
        after = ch.chat_history.current_conversation_id
        check("the first message of the run opens a new conversation",
              after not in (None, "conv_old"), str(after))
        check("which does not contain the old thread",
              all(OLD_CHAT not in m["content"]
                  for m in ch.chat_history.get_context(max_messages=20)),
              "the old thread leaked into the new conversation")

        # ---- 2. rolling summaries belong to their conversation -----------
        check("a rolling summary from another conversation is not injected",
              compaction.build_rolling_context() is None,
              str(compaction.build_rolling_context())[:120])
        compaction.ROLLING_PATH.write_text(json.dumps({
            "conversation_id": after,
            "entries": [{"ts": 1.0, "summary": "Earlier topics: ZZMARK_SAMECONV"}],
            "summarized": 40,
        }), encoding="utf-8")
        same = compaction.build_rolling_context()
        check("but its own conversation still gets it",
              same and "ZZMARK_SAMECONV" in same, str(same)[:120])
        check("a missing conversation id injects nothing",
              (ch.chat_history.start_session(),
               compaction.build_rolling_context() is None)[1],
              "rolling context survived a session boundary")
        # restore a live conversation for the pipeline checks below
        reset_history("conv_old", [])
        ch.chat_history.start_session()
        ch.chat_history.add_message("user", "carry on")

        # ---- 3. the gate: unrelated dropped, related kept ----------------
        check("filler has nothing to relate a memory to",
              await relevance.keep(FILLER, STALE_FACT) is False,
              "a bare greeting matched a memory")
        check("an unrelated question drops it too",
              await relevance.keep(UNRELATED, STALE_FACT) is False,
              "an unrelated question matched a memory")
        check("a related question keeps its memory",
              await relevance.keep(RELATED, SCHED_FACT) is True,
              "the gate dropped a memory that fits")

        provider = FakeProvider(["ok", "ok", "ok", "ok"], provider_id="mock")
        import backend.providers.registry as provider_registry
        provider_registry.get_provider = lambda: provider

        text = await chat_once(ws_server, provider, FILLER)
        check("a greeting's prompt carries no old conversation",
              "ZZMARK_OLDCHAT" not in text,
              "the previous thread was still attached")
        check("and no unrelated fact",
              "ZZMARK_STALE_SCREEN" not in text,
              "an unrelated fact reached the prompt")
        check("and no old session summary",
              "ZZMARK_OLDCONV" not in text,
              "an old session summary reached the prompt")

        text = await chat_once(ws_server, provider, RELATED)
        check("but a related question still gets its fact",
              "ZZMARK_SCHED" in text,
              "the gate starved a turn that had a relevant memory")
        check("while the unrelated one stays out",
              "ZZMARK_STALE_SCREEN" not in text,
              "gating was all-or-nothing")

        # ---- 4. the profile no longer repeats the facts ------------------
        user_profile.PROFILE_PATH.write_text(json.dumps({
            "preferences": [STALE_FACT, "Prefers dark mode."],
            "rituals": [], "hours": {"start": "07:00", "end": "22:00"},
            "tone": "friendly", "updated": 1.0,
        }), encoding="utf-8")
        profile_ctx = user_profile.build_profile_context() or ""
        check("a fact copied into preferences is not repeated",
              "ZZMARK_STALE_SCREEN" not in profile_ctx,
              "the profile injected a fact a second time")
        check("but a real preference survives",
              "dark mode" in profile_ctx, profile_ctx[:160])

        # ---- 5. the shipped threshold is repaired on load ----------------
        config_mod.SETTINGS_PATH.write_text(json.dumps({
            "memory": {"min_similarity": 0.05},
        }), encoding="utf-8")
        fresh = config_mod._Config()
        fresh.load()
        check("the old 0.05 default is migrated",
              fresh.get("memory", "min_similarity") == 0.35,
              str(fresh.get("memory", "min_similarity")))
        config_mod.SETTINGS_PATH.write_text(json.dumps({
            "memory": {"min_similarity": 0.5},
        }), encoding="utf-8")
        kept = config_mod._Config()
        kept.load()
        check("a value the user chose is left alone",
              kept.get("memory", "min_similarity") == 0.5,
              str(kept.get("memory", "min_similarity")))

        # Leave the sandbox store closed: Windows keeps a handle on the sqlite
        # file otherwise, and the temp directory cannot then be removed.
        try:
            vs.vector_store._conn.close()
            vs.vector_store._conn = None
        except Exception:
            pass

    # ---- 6. the run itself starts a session ------------------------------
    main_src = (Path(ROOT) / "backend" / "main.py").read_text(
        encoding="utf-8", errors="replace")
    check("startup closes the previous conversation",
          "chat_history.start_session()" in main_src,
          "a new run would carry on the old thread again")

    # ---- 7. the dashboard adopts the backend's view ----------------------
    page_src = (Path(ROOT) / "dashboard" / "src" / "app" / "chat" /
                "page.tsx").read_text(encoding="utf-8", errors="replace")
    check("the chat page loads the conversation the model is using",
          "chat.history" in page_src and "setMessages(msgs.length" in page_src,
          "the screen and the model can disagree again")

    if fails:
        print(f"FAIL: {len(fails)} problem(s)")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: session scope, rolling scope, relevance gate, profile dedup")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
