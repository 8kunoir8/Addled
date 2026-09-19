"""The reply comes back in the language the user wrote in.

Asked in Indonesian, Addled answered in English. The rule existed — it was one
sentence in the system prompt — and it lost to placement: `_call_prompt_tools`
puts thousands of tokens of English tool documentation between the rule and the
answer. So the final thing the model read before generating was English, and it
answered in English.

The catalogue now travels in its own message with the user's question after it
(glued on, a follow-up made the model re-answer its previous turn — see the note
in tool_loop.py), so the directive sits at the end of the question, after the
catalogue.

The failure was intermittent, which is why it is worth a suite rather than a fix:
"halo, apa kabar? kamu bisa bantu aku apa aja ya?" came back in Indonesian while
"coba jelaskan apa yang kamu bisa lakukan sekarang dengan kemampuan dan alat yg
ada ?" — a longer answer, more influenced by the catalogue — came back in English.

What is asserted:
  1. the reply language is detected for the languages this app answers in, and
     left unstated (None) rather than guessed when it is not clear;
  2. the directive rides the message the model reads last and the catalogue is
     the message before it — a test that only checks presence would pass with
     the old placement, where both were buried behind the catalogue;
  3. English gets no directive, because the local model has 8192 tokens and a
     no-op instruction spends them;
  4. the directive is not stacked once per tool round, since `full_messages` is
     reused across rounds.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_reply_language.py
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


# The message that was answered in English, and one that was not, so the suite
# keeps testing the reported case.
INDONESIAN = ("coba jelaskan apa yang kamu bisa lakukan sekarang dengan "
              "kemampuan dan alat yg ada ?")
INDONESIAN_SHORT = "halo, apa kabar? kamu bisa bantu aku apa aja ya?"
ENGLISH = "which files define the skill registry in this repo?"
SPANISH = "hola, puedes explicar que puedes hacer con tus herramientas?"
JAPANESE = "これは何ができますか"
RUSSIAN = "что ты умеешь делать"


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
    """Records what it was sent. `prompt tools` path: no native tool support."""

    supports_vision = False
    provider_id = "mock"

    def __init__(self, replies=("ok",)):
        self.replies = list(replies)
        self.payloads = []

    async def chat(self, messages, **kwargs):
        self.payloads.append(messages)
        return _Result(self.replies.pop(0) if self.replies else "ok")


def last_user_content(payload) -> str:
    for message in reversed(payload):
        if message.get("role") == "user":
            return message.get("content") or ""
    return ""


async def main() -> int:
    import backend.config as config_mod
    import backend.language as language
    import backend.memory.chat_history as ch
    import backend.memory.compaction as compaction
    import backend.memory.facts as facts
    import backend.memory.journal as journal
    import backend.memory.session_summary as session_summary
    import backend.memory.user_profile as user_profile
    import backend.memory.vector_store as vs
    import backend.skills.tool_loop as tool_loop
    import backend.ws_server as ws_server

    # ---- 1. detection --------------------------------------------------
    cases = [
        (INDONESIAN, "id", "the message that was answered in English"),
        (INDONESIAN_SHORT, "id", "the message that was answered correctly"),
        (ENGLISH, "en", "an English question"),
        (SPANISH, "es", "a Spanish question"),
        (JAPANESE, "ja", "kana, read off the script"),
        (RUSSIAN, "ru", "cyrillic, read off the script"),
    ]
    for text, expected, label in cases:
        got = language.detect(text)
        check(f"detects {expected} ({label})", got == expected, f"got {got}")

    # Short casual Indonesian is one hit, below the two-hit bar — None is the
    # honest answer there, and any wrong language is not.
    slangy = language.detect("yoo apakabar ?")
    check("casual slang is not guessed at", slangy in (None, "id"),
          f"got {slangy}")
    check("and is never mistaken for another language",
          slangy not in ("es", "fr", "de", "pt", "it"), f"got {slangy}")

    # ---- 2. the directive ---------------------------------------------
    english_directive = language.reply_directive(ENGLISH)
    check("English gets no directive (the local model has 8192 tokens)",
          english_directive == "", repr(english_directive))
    id_directive = language.reply_directive(INDONESIAN)
    check("Indonesian is named in the directive",
          "Indonesian" in id_directive, id_directive[:120])
    check("and the trap is spelled out",
          "English" in id_directive, id_directive[:200])
    unknown = language.reply_directive("zzz qqq")
    check("an undetectable message still states the rule",
          "same language" in unknown, unknown[:120])

    # ---- 3. placement: the catalogue in its own message, the directive last ----
    provider = FakeProvider()
    labelled = [{"role": "user", "content": INDONESIAN}]
    await tool_loop._call_prompt_tools(provider, labelled,
                                       only={"read_file"},
                                       reply_directive=id_directive)
    payload = provider.payloads[-1]
    sent = last_user_content(payload)
    check("the catalogue no longer shares the user's message",
          "read_file(" not in sent,
          "the catalogue is glued to the question again")
    check("it is the message immediately before the question",
          len(payload) >= 2
          and "read_file(" in str(payload[-2].get("content") or ""),
          str([m.get("role") for m in payload]))
    check("so the question is what the model reads last",
          sent.startswith(INDONESIAN), sent[:80])
    check("with the directive at the end of it, after the catalogue",
          id_directive in sent, sent[-200:])
    check("and the caller's message list is left alone",
          labelled[0]["content"] == INDONESIAN and len(labelled) == 1,
          "the question was rewritten in place")

    # ---- 4. no stacking across rounds ----------------------------------
    reused = [{"role": "user", "content": INDONESIAN}]
    first = tool_loop._with_directive(reused, id_directive)
    second = tool_loop._with_directive(reused, id_directive)
    check("the caller's message list is not mutated",
          reused[0]["content"] == INDONESIAN,
          "the directive was appended in place")
    check("each round carries exactly one directive",
          first[-1]["content"].count(id_directive) == 1
          and second[-1]["content"].count(id_directive) == 1,
          "the directive stacked")
    check("a non-user last message is left alone",
          tool_loop._with_directive(
              [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "ok"}],
              id_directive)[-1]["content"] == "ok",
          "the directive was attached to the assistant's turn")

    # ---- 5. end to end, through the pipeline ---------------------------
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        ch.HISTORY_PATH = tmp_path / "chat_history.json"
        facts.FACTS_PATH = tmp_path / "facts.json"
        journal.JOURNAL_DIR = tmp_path / "journal"
        session_summary.SUMMARIES_PATH = tmp_path / "session_summaries.json"
        compaction.ROLLING_PATH = tmp_path / "rolling_summary.json"
        user_profile.PROFILE_PATH = tmp_path / "user_profile.json"
        vs.DB_PATH = tmp_path / "vectors.db"
        vs.vector_store = vs.VectorStore()
        config_mod.SETTINGS_PATH = tmp_path / "settings.json"
        ch.HISTORY_PATH.write_text(json.dumps({
            "conversations": {}, "current_conversation": None}),
            encoding="utf-8")
        facts.FACTS_PATH.write_text("[]", encoding="utf-8")

        live = FakeProvider()
        await ws_server.run_chat_pipeline(INDONESIAN, record=False,
                                          announce=False, provider=live)
        payload = live.payloads[0]
        turn = last_user_content(payload)
        check("the pipeline sends the Indonesian directive",
              "Indonesian" in turn, turn[-200:])
        check("on the question, which is the last thing sent",
              turn.startswith(INDONESIAN) and "read_file(" not in turn,
              turn[:200])
        check("with the catalogue in the message before it",
              len(payload) >= 2
              and "read_file(" in str(payload[-2].get("content") or ""),
              "the directive is not last in the turn being answered")

        live_en = FakeProvider()
        await ws_server.run_chat_pipeline(ENGLISH, record=False,
                                          announce=False, provider=live_en)
        check("an English turn carries no directive at all",
              "Reply language" not in last_user_content(live_en.payloads[0]),
              "a no-op instruction is spending context")

        try:
            vs.vector_store._conn.close()
            vs.vector_store._conn = None
        except Exception:
            pass

    if fails:
        print(f"FAIL: {len(fails)} problem(s)")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: reply language detected, and stated after the tool catalogue")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
