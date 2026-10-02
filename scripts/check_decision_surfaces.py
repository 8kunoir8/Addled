"""A decision must be answerable wherever it is shown.

Three surfaces used to show a prompt and give no way to answer it:

* **The bots.** Telegram and Discord drew approval buttons but nothing for a
  question, and WhatsApp — which has no tappable buttons at all — could only
  describe a permission request in prose. A user told "permission needed" on
  their phone had to go and find the dashboard.
* **The floating character.** A permission request or a question arrived as
  plain text in the bubble with no buttons, so the one surface that is
  *always* on screen was the one that could not act on a prompt.

Two rules decide whether a bridge can answer, and both are checked here because
getting either wrong produces a button that silently does nothing:

1. **Only the chat that was asked may answer.** The same origin rule the
   approvals use. A forwarded button must not release another conversation's
   decision.
2. **The wording travels with the reply.** `callback_data`/`custom_id` are
   capped at 64/100 bytes, so an option's *index* travels and the bridge
   recovers the label from what it sent. A bridge that assumed the label fitted
   in the button would offer two identical stubs for a long choice.

And one about the character specifically: a decision bubble must NOT auto
dismiss or close on a body click. A prompt that answers itself by fading away
is worse than no prompt — the work behind it stays blocked and the user never
saw why.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_decision_surfaces.py
"""

from __future__ import annotations

import os
import re
import sys

# The findings name the bubble's close glyph, and a Windows console defaults to
# cp1252 — without this a FAILURE report crashed while printing the reason it
# failed, which is the worst possible time to lose the output.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

BOTS = os.path.join(ROOT, "bots")
SERVER = os.path.join(ROOT, "backend", "ws_server.py")
BUBBLE = os.path.join(ROOT, "backend", "character", "chat_bubble.py")
MAIN = os.path.join(ROOT, "backend", "main.py")

fails: list[str] = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}" if detail else label)

def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()

def main() -> int:
    for path in (SERVER, BUBBLE, MAIN):
        if not os.path.isfile(path):
            print(f"missing file: {path}")
            return 2

    server = read(SERVER)
    bubble = read(BUBBLE)
    mainpy = read(MAIN)

    # ---- 1. the backend hands the bridge what it needs ----------------------
    check("chat.send reports the questions it left waiting",
          'result["pendingQuestions"] = _questions_for(params)' in server)
    check("the questions are scoped to the conversation that asked",
          re.search(r"open_questions\(source=source,\s*\n\s*conversation=conversation\)",
                    server) is not None,
          "passing an empty conversation would leak every chat's question "
          "into every reply")
    check("the payload carries the wording, not just an id",
          re.search(r'_questions_for.*?"question": entry\["question"\]', server, re.S)
          is not None,
          "a bridge has no card to fall back on; an id alone gives it no label")
    check("the payload carries the choices",
          '"options": entry.get("options")' in server)
    check("a conversation-less call gets no questions",
          server.count("if not conversation:") >= 2,
          "returning every question on the machine would leak across chats")

    # ---- 2. each bridge can show and answer ---------------------------------
    for name in ("telegram-bot.js", "discord-bot.js", "whatsapp-bot.js"):
        path = os.path.join(BOTS, name)
        if not os.path.isfile(path):
            check(f"{name} exists", False)
            continue
        src = read(path)
        label = name.replace("-bot.js", "")

        check(f"{label}: reads pendingQuestions",
              "pendingQuestions" in src)
        check(f"{label}: has a question prompt",
              "questionPrompt" in src)
        # The card has to actually be sent.
        #
        # Anchored to the `questionPrompt(...)` call within reach of its guard,
        # NOT to "the first if-block mentioning questions": a greedy regex
        # matched the mention-path block and reported the card present after it
        # had been removed from the slash-command path.
        #
        # Counting rather than matching once, because Discord has TWO sites (the
        # slash command and the mention path) and each is independently
        # sufficient — removing one is not a defect. Comparing the counts is
        # what makes "a guard that draws nothing" fail while "one of two sites
        # gone" correctly does not.
        guards = len(re.findall(
            r"if \((?:questions\.length|questions\[0\]\?\.question_id)\) \{", src))
        draws = (len(re.findall(r"questionPrompt\(", src))
                 - len(re.findall(r"function questionPrompt\(", src)))
        check(f"{label}: the question card is actually sent",
              guards >= 1 and draws >= guards,
              f"{guards} guard(s) that draw a question, {draws} drawn — a guard "
              f"with no card is a question the user cannot answer")
        check(f"{label}: remembers what it asked",
              # Call sites only. A bare `rememberQuestion(...)` also matches the
              # definition line (`function rememberQuestion(channelId, q)`),
              # which is how removing every call while keeping the definition
              # still looked like a pass.
              len(re.findall(r"(?<!function )rememberQuestion\([^)]*\)", src)) >= 1
              and "openQuestions" in src,
              "without this an answer cannot be tied to its question")
        check(f"{label}: the remember sits with the prompt that needs it",
              re.search(r"rememberQuestion\([\s\S]{0,400}?questionPrompt\(", src)
              is not None,
              "a remember elsewhere in the file does not help a card that was "
              "sent without one")
        check(f"{label}: answers through question.answer",
              "question.answer" in src)
        check(f"{label}: resumes the work after answering",
              "question.answer" in src and "chat.send" in src)
        check(f"{label}: tells the model the answer is an answer",
              "This is my answer to the question you asked" in src,
              "otherwise the model reads it as a fresh instruction")
        check(f"{label}: clears the question once settled",
              "takeQuestion" in src)
        # Presence is not behaviour. The resume has to be INSIDE the answer
        # helper and gated on the answer being accepted — a bridge that sends
        # `question.answer` and stops has settled a question without the model
        # ever learning the answer, which reads to the user as "it asked and
        # then ignored me".
        #
        # The body runs from the helper to the next top-level function, not to
        # the first `}`: the helper contains callbacks whose braces close early.
        helper = re.search(r"async function answerQuestion\(", src)
        body = ""
        if helper:
            rest = src[helper.end():]
            nxt = re.search(r"\nasync function |\nfunction ", rest)
            body = rest[:nxt.start()] if nxt else rest
        check(f"{label}: the resume is part of the answer helper",
              "chat.send" in body and "question.answer" in body,
              "settling and resuming must be one path, not two that can drift")
        if "chat.send" in body and "question.answer" in body:
            check(f"{label}: it resumes only after the answer is accepted",
                  body.index("question.answer") < body.index("chat.send"),
                  "resuming first sends a turn whose answer is not stored yet")
            # The guard is what makes the order meaningful. Without it the
            # resume runs even when the question was refused or already settled,
            # and the model is told about an answer nobody accepted.
            check(f"{label}: the resume is gated on the answer succeeding",
                  re.search(r"if \(!settled\?\.success\)\s*(?:return|\{)", body)
                  is not None,
                  "an unguarded resume continues the work on a refused answer")

        # Only the chat that was asked may answer.
        if label == "telegram":
            check("telegram: a callback from another chat is refused",
                  "This is not the chat that asked." in src)
            check("telegram: question choices travel as an index",
                  re.search(r'callback_data:\s*`qa:\$\{id\}:\$\{index\}`', src)
                  is not None,
                  "a long option would not fit in 64 bytes and would be truncated")
        if label == "discord":
            check("discord: question choices travel as an index",
                  re.search(r'setCustomId\(`qa:\$\{id\}:\$\{i \+ offset\}`\)', src)
                  is not None)
            check("discord: the question card is a follow-up",
                  "questionRows(q)" in src)
        if label == "whatsapp":
            # No buttons exist here, so the mechanism must be different.
            check("whatsapp: choices are numbered",
                  re.search(r'lines\.push\(`\$\{i \+ 1\}\. \$\{label\}`\)', src)
                  is not None,
                  "without buttons the only way to offer a choice is a number")
            check("whatsapp: a number is read back as the choice",
                  re.search(r"classifyQuestionReply[\s\S]*?options\[index\]",
                            src) is not None,
                  "the helper existing is not enough — it has to turn the "
                  "number into the option")
            check("whatsapp: a too-large number stays text",
                  "may be the answer itself" in src,
                  "'how many retries?' -> '3' must not be read as a missing option")
            check("whatsapp: questions and approvals do not share a map",
                  "openQuestions" in src and "waiting" in src,
                  "one is a number or free text, the other three fixed words")

    # ---- 3. the floating character ------------------------------------------
    check("the bubble can show a decision",
          "def show_decision" in bubble and "buttons" in bubble)
    # A decision must not auto-dismiss IMMEDIATELY, and must not be immortal
    # either. Round 1 removed the timeout, which left an unanswered prompt with
    # no exit at all: `fade_out` and the click both refused, so it sat on top of
    # the character for the rest of the session. The deadline is long but real,
    # and `_timeout` is what honours it without re-opening user dismissal.
    check("a decision does not dismiss itself like a message",
          "duration_s=self.DECISION_TIMEOUT_S" in bubble,
          "a prompt that fades like a message leaves the work blocked unseen")
    check("a decision still has a finite deadline",
          "DECISION_TIMEOUT_S" in bubble
          and re.search(r'def _timeout[\s\S]{0,900}?self\._clear_actions\(\)',
                        bubble) is not None,
          "refusing every dismissal made the prompt impossible to clear")
    check("the timeout is wired to the timer",
          "self._timer.timeout.connect(self._timeout)" in bubble,
          "a timer connected to a method that refuses is decorative")
    check("a decision is not closed by clicking the bubble body",
          re.search(r"if self\._decision_open:\s*\n\s*event\.ignore\(\)", bubble)
          is not None,
          "the whole point of putting it on the character is that it is visible")
    check("a decision is visually distinct",
          "_decision_open" in bubble and "210, 153, 34" in bubble)
    check("the answer row is cleared between uses",
          "def _clear_actions" in bubble,
          "otherwise dead buttons accumulate on a reused bubble")
    check("callbacks are bound per button",
          "fn=callback" in bubble,
          "a late-binding closure sends the last verb for every button")

    # ---- 4. the wiring -------------------------------------------------------
    check("the backend can invoke a handler in-process",
          "async def call(self, method" in server,
          "the character is not a socket client and has no other way to answer")
    check("the server has a UI notifier hook",
          "def set_ui_notifier" in server and "def _notify_ui" in server)
    check("the notifier is called for every broadcast",
          "self._notify_ui(method, params)" in server,
          "a future notification should not have to remember to wire itself up")
    check("main.py attaches the notifier",
          "set_ui_notifier(_on_ui_notification)" in mainpy)
    check("permissions reach the character",
          'method == "action.approvalRequest"' in mainpy)
    check("questions reach the character",
          'method == "question.ask"' in mainpy)
    check("the bubble's answer goes through the same handler as the dashboard's",
          "get_server().call(" in mainpy)
    check("the answer is dispatched onto the WS loop",
          "run_coroutine_threadsafe" in mainpy,
          "a Qt click handler is not on the asyncio loop")

    # ---- 5. the defects the audit found -------------------------------------
    audit_fixes()

    print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


def audit_fixes() -> None:
    """The defects the 2026-10-02 audit found, each pinned so it cannot return.

    Every one of these was live in shipped code and demonstrated against the
    running app. They are grouped here rather than spread through the file
    because what they have in common is the *class* of mistake: a surface that
    settles a decision without continuing the work, or a message that vanishes
    when it cannot be used as an answer.
    """
    server = read(os.path.join("backend", "ws_server.py"))
    bubble = read(os.path.join("backend", "character", "chat_bubble.py"))
    mainpy = read(os.path.join("backend", "main.py"))
    store = read(os.path.join("dashboard", "src", "lib", "questionsStore.ts"))
    chatpage = read(os.path.join("dashboard", "src", "app", "chat", "page.tsx"))

    # 1. The bubble settled a question without resuming the work. Same bug the
    #    bots had, missed on the fourth surface.
    helper = re.search(r"async def _answer_question.*?\n    async def ", mainpy, re.S)
    check("the character's answer resumes the work",
          helper is not None and "chat.send" in helper.group(0),
          "question.answer alone settles the queue and tells the model nothing")
    if helper is not None:
        check("on the character the resume follows the answer",
              "question.answer" in helper.group(0)
              and helper.group(0).index("question.answer")
              < helper.group(0).index("chat.send"))

    # 2. question.answer was unscoped: any id settled any conversation's
    #    question. Live-demonstrated by answering a Telegram question as
    #    `dashboard`.
    check("question.answer checks the origin",
          "_question_origin_mismatch" in server,
          "the read path was scoped and the write path was not")
    check("question.dismiss checks the origin too",
          re.search(r"question_dismiss.*?_question_origin_mismatch", server, re.S)
          is not None)
    check("the origin check fails closed",
          "The question's origin could not be verified." in server,
          "an error in the check must refuse, not allow")
    for name, src in (("telegram", read(os.path.join("bots", "telegram-bot.js"))),
                      ("discord", read(os.path.join("bots", "discord-bot.js"))),
                      ("whatsapp", read(os.path.join("bots", "whatsapp-bot.js")))):
        check(f"{name}: the answer names its origin",
              re.search(r"question\.answer'[\s\S]{0,200}?conversation", src)
              is not None)
    check("the dashboard's answer names its origin",
          re.search(r"question\.answer[\s\S]{0,200}?\.\.\.origin", store) is not None)

    # 3. A refused answer was reported as success, and the message was dropped.
    tg = read(os.path.join("bots", "telegram-bot.js"))
    dc = read(os.path.join("bots", "discord-bot.js"))
    check("telegram does not claim success for a refused answer",
          re.search(r"if \(!settled\?\.success\)[\s\S]{0,400}?Answered:",
                    tg) is None,
          "'✅ Answered' for a refusal tells the user work resumed when it did not")
    check("discord does not claim success for a refused answer",
          re.search(r"if \(!settled\?\.success\)[\s\S]{0,400}?Answered:",
                    dc) is None)
    check("telegram forwards the message when the answer is refused",
          "Sending your message as a normal request instead" in tg,
          "the text must not vanish; it is a turn the user meant to send")
    check("discord forwards the message when the answer is refused",
          "Sending your message as a normal request instead" in dc)
    check("telegram re-remembers only when the answer never landed",
          re.search(r"catch \(e\) \{[\s\S]{0,1200}?rememberQuestion", tg)
          is not None
          and re.search(r"answerQuestion\([\s\S]{0,1200}?return settled;", tg)
          is not None,
          "a resume failure after a successful answer must not re-arm a "
          "question the backend has already closed")
    check("discord re-remembers on a thrown failure",
          re.search(r"catch \(e\) \{[\s\S]{0,1200}?rememberQuestion", dc)
          is not None)

    # 4. WhatsApp re-remembered a question that could never be answered, which
    #    swallowed every later message in that chat.
    wa = read(os.path.join("bots", "whatsapp-bot.js"))
    check("whatsapp does not re-remember a terminally refused question",
          re.search(r"const terminal =", wa) is not None
          and re.search(r"if \(!terminal\)\s*\{", wa) is not None,
          "guarding only on `expired` missed the origin refusal, so a "
          "permanent refusal still consumed every later message")

    # 5. An approval's "yes" was consumed as a question's answer.
    check("whatsapp prefers a pending approval over an open question",
          "approvalWord" in wa and "pendingApproval && approvalWord" in wa,
          "a question accepts any text, so it swallowed the approval's 'yes'")

    # 6. The bubble lost a decision: replaced by a message, replaced by a
    #    second decision, or escaped via ✕ leaving live buttons behind.
    # The guard is `if self._decision_open:` … `return`, with a log line in
    # between, so the pattern allows for anything up to the return rather than
    # assuming they are adjacent. A message is QUEUED, not dropped: returning
    # early without storing it lost every insight and reply that arrived while a
    # prompt was on screen.
    check("a message does not clear an open decision",
          re.search(r"def show_message[\s\S]{0,1400}?if self\._decision_open:"
                    r"[\s\S]{0,300}?return", bubble) is not None,
          "a proactive insight arriving mid-prompt destroyed Allow/Deny")
    check("a message arriving during a decision is queued, not dropped",
          "_queued_messages" in bubble
          and re.search(r"def show_message[\s\S]{0,1400}?_queued_messages\.append",
                        bubble) is not None,
          "returning early without storing the text lost it permanently")
    check("the close button does not dismiss an open decision",
          re.search(r"def fade_out\(self\):[\s\S]{0,900}?if self\._decision_open:"
                    r"[\s\S]{0,200}?return", bubble) is not None,
          "a silent escape from a permission prompt, leaving stale buttons")
    # Round 1 required `show_thinking` to CLEAR a decision, because a grey bubble
    # holding live buttons is a contradiction. Round 2 pointed out that clearing
    # destroys the prompt — the callback is dropped and the action stays queued.
    # Both are true, so the thinking bubble yields: a decision is what needs
    # attention and is not replaced by it.
    check("a decision is not destroyed by a thinking bubble",
          re.search(r"def show_thinking[\s\S]{0,900}?if self\._decision_open:"
                    r"[\s\S]{0,200}?return", bubble) is not None,
          "clearing it dropped Allow/Deny with nothing sent")
    check("thinking does not leave live buttons on a grey bubble",
          re.search(r"def show_thinking[\s\S]{0,900}?self\._thinking = True", bubble,
                    re.S) is not None
          and bubble.index("if self._decision_open:",
                           bubble.index("def show_thinking"))
          < bubble.index("self._thinking = True",
                         bubble.index("def show_thinking")),
          "the guard must come before the state changes it protects")
    check("a second decision is queued, not dropped",
          "_queued.append" in bubble and "def _advance" in bubble)
    check("the queue is advanced when a decision ends",
          re.search(r"def _on_action[\s\S]{0,300}?_advance\(\)", bubble) is not None)

    # 7. Restored questions lost the ttl the live card showed.
    check("questions.list puts a ttl on every item",
          re.search(r"\*\*item, \"ttl\": ttl", server) is not None,
          "one shape, two payloads — the restored card silently dropped the "
          "expiry it promised when pushed")

    # 8. `recent_outcomes` was documented as feeding the next turn and was
    #    called by nothing, so an expired question was simply forgotten.
    check("a finished question is reported to the next turn",
          "recent_outcomes(" in server,
          "the feature was documented and dead; a lapsed card was re-asked")
    check("the report is consumed so it is not replayed",
          "forget_outcomes(" in server)

    # 9. attendance_gaps() could not fail: it subtracted a derived complement.
    cs = read(os.path.join("backend", "chat_sources.py"))
    check("the unattended set is declared, not derived",
          re.search(r"^UNATTENDED = frozenset\(\{", cs, re.M) is not None
          and "frozenset(SOURCES) - ATTENDED" not in cs,
          "subtracting ATTENDED's own complement is tautologically empty, so "
          "the gap check could never fire")

    # 10. Telegram's media path showed no decision the turn had raised.
    media = re.search(r"async function handleMedia[\s\S]*?\n\}", tg)
    check("telegram's media path surfaces pending questions",
          media is not None and "pendingQuestions" in media.group(0),
          "a question raised from a photo was never shown and expired")

    # 11. The chat page rendered every conversation's questions.
    check("the chat page filters to its own conversation",
          "questionsFor('dashboard')" in chatpage,
          "a Telegram question would render here with a resume aimed at "
          "Telegram's thread")

    # 13. Typing at the character must ANSWER an open question, not start a new
    #     request. It is the obvious thing to try when the bubble asks something
    #     and it silently sent a fresh turn while the question expired.
    check("the character prompt asks the bubble what is open",
          "bubble.open_question()" in mainpy,
          "without this the typed text became a new request and the question "
          "expired, with nothing saying so")
    check("the typed answer goes through the same helper as a button",
          re.search(r"if held:\s*\n\s*asyncio\.run_coroutine_threadsafe\(\s*\n"
                    r"\s*_answer_question\(held, answer\)", mainpy) is not None,
          "two paths that can drift; one must settle AND resume")
    check("there is ONE answer helper that settles then resumes",
          len(re.findall(r"async def _answer_question", mainpy)) == 1
          and "_answer_question(record" in mainpy,
          "two copies would drift, and the half that is easy to forget is the "
          "resume")
    check("the prompt shows the question being answered",
          re.search(r'prompt = \(held\.get\("question"\)', mainpy) is not None,
          "a generic box means the user cannot see what they are answering")
    check("the bubble keeps the record the answer needs",
          "_open_decision" in bubble and "payload=payload.get(\"record\")"
          in mainpy,
          "the answer has to name its conversation; the display text alone "
          "cannot")
    check("the store prunes an error when a question leaves",
          re.search(r"export function removeQuestion[\s\S]{0,600}?delete rest",
                    store) is not None)
    check("restore prunes errors for questions that are gone",
          re.search(r"export function setQuestions[\s\S]{0,700}?stale", store)
          is not None)


if __name__ == "__main__":
    sys.exit(main())
