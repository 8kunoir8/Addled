"""Can the agent be OFFERED its own skills?

A prompt-based provider (the local model) never sees all 141 skills. It sees the
ten that `filter_for_query` selects for the question, and four of those slots are
always the core utilities. A skill that is never selected is, from the model's
point of view, a skill that does not exist — and nothing measured this.

The test deliberately does NOT search by skill name. Typing "excel_write" would
pass trivially and prove nothing; the question is whether a user saying
"make me a spreadsheet" is offered the tool that makes one.

Run with the dev bundle, from the project root:

    .\\python-bundle\\python.exe -s .\\scripts\\check_reachability.py

Exits non-zero when a skill no realistic phrasing can reach.
"""

import os
import sys

# Resolve the repo from THIS file, the way every other check does. It was
# pinned to an absolute path in another worktree, so the suite tested whatever
# copy happened to live there - and would not run at all on a machine without
# it. A check that can silently measure a different tree is worse than no
# check: it reports on code the user is not running.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.skills.registry import skill_registry as R  # noqa: E402

CORE = {"web_search", "read_file", "write_file", "run_command"}

# Phrasings a person would actually type. Every skill that has a plausible plain
# -language ask is listed here; the point is to give each skill its BEST chance,
# so a skill that still fails is unreachable in practice, not just here.
PHRASINGS: dict[str, list[str]] = {
    # -- documents (the v1.0.25 group, the reason this check exists) ----------
    "word_read": ["read this word document", "open the docx and tell me what it says",
                  "summarise this report"],
    "word_create": ["write a word document", "make me a docx report",
                    "create a document saying hello"],
    "word_edit": ["edit the word document", "update the docx"],
    "excel_read": ["read this excel file", "what is in the spreadsheet",
                   "open the xlsx and show me the data"],
    "excel_write": ["make me a spreadsheet", "create an excel file",
                    "put these numbers in a spreadsheet"],
    "excel_sheets": ["list the sheets in this workbook", "what sheets are in the xlsx"],
    "pptx_read": ["read this powerpoint", "what is in the presentation",
                  "summarise the slides"],
    "pptx_create": ["make a powerpoint", "create a presentation", "build me a slide deck"],
    "pptx_add_slide": ["add a slide to the presentation", "add another slide"],
    "pdf_read": ["read this pdf", "what does the pdf say", "summarise the pdf"],
    "pdf_create": ["make a pdf", "create a pdf from this text", "turn this into a pdf"],
    "pdf_pages": ["how many pages is this pdf", "count the pages"],
    "pdf_edit": ["rotate the pdf page", "reorder the pages of this pdf"],
    "pdf_merge": ["combine these pdfs", "merge the pdf files into one"],
    "pdf_extract": ["pull pages out of the pdf", "extract page 2 of the pdf"],
    "pdf_redact": ["remove the account number from this pdf", "redact the names in the pdf",
                   "delete text from this pdf permanently"],
    "pdf_redact_verify": ["check the redaction worked", "confirm text is gone from the pdf"],
    "convert_to_pdf": ["turn this word doc into a pdf", "convert the docx to pdf"],
    "convert_from_pdf": ["convert this pdf to a word document", "turn the pdf into text"],
    # -- files ---------------------------------------------------------------
    "read_file": ["read the file", "show me foo.txt"],
    "write_file": ["write a file", "save this to a text file"],
    "list_dir": ["what files are in this folder", "list the directory"],
    "search_files": ["find files named config", "search for a file"],
    "search_in_files": ["find which file mentions database", "grep for the word token"],
    "search_passages": ["search code passages for the function", "find the code block containing database"],
    "delete_file": ["delete the file", "remove temp.txt"],
    "move_file": ["move the file", "rename this file"],
    "copy_file": ["copy the file", "make a duplicate of it"],
    "create_dir": ["make a new folder", "create a directory"],
    "file_info": ["how big is this file", "when was it modified"],
    # -- system --------------------------------------------------------------
    "run_command": ["run this command", "execute the script"],
    "screenshot": ["take a screenshot", "grab my screen"],
    "get_screen_size": ["what is my screen resolution", "how big is my monitor"],
    "clipboard_read": ["what is in my clipboard", "read the clipboard"],
    "get_clipboard": ["what is in my clipboard", "read the clipboard"],
    "clipboard_write": ["copy this to my clipboard", "put this on the clipboard"],
    "set_clipboard": ["copy this to my clipboard", "put this on the clipboard"],
    "volume": ["set the volume", "turn the sound up", "mute the audio"],
    "set_volume": ["set the volume", "turn the sound up", "mute the audio"],
    "brightness": ["dim the screen", "set brightness"],
    "set_brightness": ["dim the screen", "set brightness"],
    "lock_screen": ["lock my computer", "lock the screen"],
    # -- desktop control (destructive: off by default, but must be reachable) --
    "desktop_click": ["click on the screen at 100 200", "click that button with the mouse"],
    "desktop_type": ["type this text into the window", "type hello world"],
    "desktop_hotkey": ["press ctrl c", "send the hotkey alt tab"],
    "desktop_scroll": ["scroll down the page", "scroll up a bit"],
    "desktop_move_mouse": ["move the mouse to the corner"],
    "desktop_drag": ["drag that icon to the other side"],
    # -- web (web_search is core, always offered; web_fetch is not) ----------
    "web_search": ["search the web for rust news"],
    "web_fetch": ["fetch the page at example.com", "get the contents of this url"],
    # -- windows -------------------------------------------------------------
    "list_windows": ["what windows are open", "list open applications"],
    "focus_window": ["switch to notepad", "bring the browser to the front"],
    "resize_window": ["resize the notepad window", "make this window bigger"],
    "close_window": ["close the notepad window", "shut the browser window"],
    # -- browser -------------------------------------------------------------
    "browser_navigate": ["open the website example.com", "go to google.com"],
    "browser_extract": ["get the text from this page", "scrape the article"],
    "browser_click": ["click the login button on the page"],
    "browser_type": ["type my email into the form"],
    # -- code ----------------------------------------------------------------
    "code_read": ["read the code", "show me the source of main.py"],
    "code_edit": ["change the code", "edit this function"],
    "verify_code": ["check if my code compiles", "run the tests", "does this project build"],
    # -- integration ---------------------------------------------------------
    "calendar_add": ["add a dentist appointment on friday", "put this on my calendar"],
    "calendar_list": ["what is on my calendar today", "my schedule this week"],
    "calendar_delete": ["cancel the meeting on my calendar", "remove that appointment"],
    "email_list": ["check my email", "what is in my inbox", "any unread mail"],
    "email_search": ["search my email for invoices", "find the email from sam"],
    "email_send": ["send an email to sam", "email the team about the meeting"],
    "transcribe_audio": ["transcribe this recording", "turn the audio into text"],
    "send_message": ["send a whatsapp to sam", "message the team on telegram"],
    "bot_status": ["are the bots connected", "what is the whatsapp status"],
    "chat_history": ["what did we talk about on whatsapp", "read that chat history"],
    # -- meta / memory / wiki / sop ------------------------------------------
    "forge_skill": ["you need to learn how to do something new", "make a new skill for this"],
    "list_forged": ["what skills have you made", "list your learned skills"],
    "find_mcp_server": ["find an mcp server for this"],
    "memory_get": ["what do you remember about me", "what is my name"],
    "memory_set": ["remember that I prefer dark mode", "note that for later"],
    "wiki_search": ["search my notes", "look in the wiki for that"],
    "wiki_read": ["read that wiki page"],
    "wiki_write": ["write a wiki page about the api", "add a note to the wiki"],
    "wiki_ingest": ["add this document to the wiki", "ingest this page into the wiki"],
    "wiki_links": ["what links to this page", "show the wiki graph"],
    "wiki_lint": ["check the wiki for broken links", "lint my notes"],
    "memory_files": ["which files do you remember", "what files have you seen"],
    "memory_link": ["link these two memories together", "connect that to this"],
    "memory_unlink": ["unlink those memories"],
    "memory_related": ["what else do you know related to that", "show related memories"],
    "memory_graph": ["show me the memory graph", "draw your knowledge graph"],
    "sop_lookup": ["how do we usually do this", "what is our procedure for deploys"],
    "sop_list": ["list the procedures", "what runbooks do we have"],
    "sop_save": ["save this as a procedure", "record this as our standard process"],
    "guidelines_status": ["which guidelines are active", "what coding guidelines are you using"],
    "ponytail_review": ["review my code against the guidelines", "do a ponytail review"],
    "search_in_files": ["find which file mentions database", "grep for the word token"],
    "search_passages": ["search code passages for the function", "find the code block containing database"],
    "browser_navigate": ["open the website example.com", "go to google.com"],
    "browser_task": ["use the browser to fill in this form", "do this task in the browser"],
    "session_open": ["start an interactive shell", "open a terminal session"],
    "session_send": ["run this in the shell session", "send this command to the session"],
    "session_read": ["read the session output"],
    "session_close": ["close the shell session"],
    "session_list": ["list my shell sessions"],
    "self_read": ["read your own source code", "show me your own code"],
    "self_propose": ["change your own code to do this", "modify yourself"],
    "self_apply": ["apply that change to yourself"],
    "self_revert": ["revert your own change"],
    "self_pending": ["what self changes are pending"],
    "swarm_note": ["leave a note for the other agents"],
    "swarm_notes": ["read the swarm notes"],
    "swarm_roster": ["show the agent roster", "what agents do I have"],
    "swarm_learn": ["teach the agent a rule"],
    # Delegation has no single obvious phrasing — a user asks for the *outcome*
    # ("have the reviewer look at this"), not for "delegation". So the list is
    # the phrasings that should reach it, which is the same standard the check
    # applies to every other skill: give it its best chance.
    "swarm_delegate": [
        "have the reviewer check this",
        "ask the researcher to look into it",
        "delegate this to an agent",
        "get another agent to do this",
        "hand this off to one of your agents",
    ],
    # Creating and renaming a desk. The wording is the user's, not the tool's:
    # nobody says "create a swarm agent definition", they say "make me an agent
    # called Scout" — the same standard the check applies everywhere else.
    "swarm_create": [
        "create an agent called Scout",
        "make me a new agent for reviewing copy",
        "add a swarm agent that writes emails",
    ],
    "swarm_rename": [
        "rename the Scout agent to Lookout",
        "call that agent something else",
    ],
    "task_schedule": ["remind me to call mum at 5pm", "schedule a task for tomorrow"],
    "task_list": ["what reminders do I have", "list my scheduled tasks"],
    "task_cancel": ["cancel that reminder"],
    # The awkward one: `ask_user` is called when the model is UNSURE, so there
    # is no user phrasing that asks for it. These are the situations where the
    # model needs it, which is the closest honest equivalent — the check wants
    # to know it can be offered, not that a user would request it by name.
    "ask_user": ["which file did you mean", "I am not sure what you mean",
                 "can you clarify", "ask me a question"],
    # -- meetings (integrations) ---------------------------------------------
    # These had no phrasing at all, so the check counted them unreachable
    # without ever asking whether they were. They are reachable, and these are
    # the asks that reach them.
    "meeting_list": ["what meetings do I have", "list my meetings",
                     "show me my meeting transcripts"],
    "meeting_get": ["get the meeting transcript", "show me that meeting",
                    "open the meeting notes"],
    "meeting_actions": ["what action items came out of the meeting",
                        "what were the action items from that meeting",
                        "pull the follow-ups from the meeting"],
    "meeting_save": ["save this meeting transcript", "save the meeting notes",
                     "store this transcript"],
    "meeting_summarise": ["summarise the meeting", "summarise this transcript",
                          "what was decided in the meeting"],
    # -- meta: asking the agent about its own skills --------------------------
    "skill_search": ["what skills do you have", "do you have a skill for pdfs",
                     "is there a skill that can do this"],
    "skill_view": ["show me the details of that skill",
                   "what can the pdf skill do",
                   "how does that skill work"],
    "skill_read": ["open the template that skill ships",
                   "read the skill's reference file"],
}


def offered_for(query: str, max_tools: int = 19) -> set[str]:
    """What a real turn would offer this query.

    The default must match `filter_for_query`'s own, or this check tests a
    configuration the app never uses. It has drifted twice: pinned at 10 while
    the code moved to 16, and then left at 16 when `skill_search`/`skill_view`
    joined `must` and the budget moved to 18. Both times it reported skills as
    unreachable that the app could reach.
    """
    return R.filter_for_query(query, max_tools=max_tools)


def check_catalogue_reaches_every_skill() -> int:
    """A skill added LATER, that this code has never seen, must still be visible.

    This is the general property, and it is the one that actually matters: the
    PHRASINGS table above can only cover the skills that existed when it was
    written, so it proves nothing about a skill the market installs tomorrow.
    Keyword scoring cannot be fixed for that case — a new skill's description is
    prose nobody has tuned, and the words that identify it are the same generic
    words every other skill uses ("use when implementing a feature"), so a
    word-overlap filter scores it zero and never offers it.

    The fix is that the prompt catalogue lists EVERY enabled skill by name and
    description regardless of scoring, so visibility does not depend on a match.
    This registers a deliberately unlikely skill — a nonsense name and a
    description that shares no word with any realistic query — and asserts it
    still appears in what the model is shown. A regression that returns to
    keyword-only disclosure fails here, on a skill that did not exist when this
    check was written.
    """
    from backend.skills.registry import SkillDefinition

    name = "zzz-synthetic-future-skill"
    R.register(SkillDefinition(
        name=name,
        description="Frobnicates the wibble when the user asks for one.",
        parameters={"type": "object", "properties": {}},
        handler=None,
        category="market",
    ))
    try:
        # A query that shares NO word with the synthetic description: keyword
        # scoring gives it zero, which is exactly the failure being guarded.
        only = R.filter_for_query("what is the weather in Oslo")
        text = R.to_prompt_tools(only)
        listed = any(line.startswith(f"- {name} ")
                     or line.startswith(f"- {name} —") for line in text.splitlines())
        print("=" * 74)
        print("FUTURE-SKILL CATALOGUE CHECK")
        print("=" * 74)
        print(f"  synthetic skill registered: {name}")
        print(f"  offered by keyword scoring: {name in only}  (expected False)")
        print(f"  present in prompt catalogue: {listed}  (expected True)")
        if not listed:
            print("  FAIL: a newly registered skill is invisible to the model — "
                  "discovery has regressed to keyword matching.")
            return 1
        print("  PASS: an unlisted, never-anticipated skill is still disclosed.")
        return 0
    finally:
        R.unregister(name)


def check_discovery_tools_always_offered() -> int:
    """The core tools, and the tools the catalogue NAMES, are offered every turn.

    Two properties in one place, because they fail the same way and for the same
    reason:

    - `to_prompt_tools` tells the model "Call `skill_view` with a name to read
      its instructions, or `skill_search` to find one by task." Those two are
      registered under category `meta` and are what a conversational turn needs
      to reach any skill at all — but they were not added to `discovery_tools`,
      so they were force-added only when a query happened to contain the word
      "skill" or "search". On "hello" the prompt pointed at two tools it did not
      then offer.
    - The core utilities are what a turn cannot work without, and they were
      droppable: `must` skipped any tool that had already scored, so with a
      budget below `len(must)` the returned set could omit `web_search`,
      `read_file` and `write_file` at once.

    Neither hard-failed, because a call is resolved against the whole registry
    rather than the offered subset — which is exactly why they went unnoticed,
    and exactly why they are worth a check: the app worked by an accident of the
    lookup, not by design, and any future check that constrains calls to the
    offered set would break it.

    Asserts the property directly: whatever the query, all of them are offered.
    """
    # `skill_read` belongs here with the other two: `skill_view` now tells the
    # model to open a skill's bundled file by name, and a tool named in a
    # result but not offered on the turn is a dead end the model cannot take.
    required = ["web_search", "read_file", "write_file", "run_command",
                "skill_search", "skill_view", "skill_read"]
    probes = ["", "hello", "i feel tired today", "what is 2+2", "thanks!",
              "convert this pdf to a spreadsheet"]
    print("=" * 74)
    print("ALWAYS-OFFERED TOOLS CHECK")
    print("=" * 74)
    print("  core utilities + the discovery tools the catalogue names, on every turn")
    missing_any = []
    for q in probes:
        offered = offered_for(q)
        missing = [n for n in required if n not in offered]
        mark = "ok" if not missing else "MISSING " + ",".join(missing)
        print(f"  {q!r:38} -> {mark}")
        if missing:
            missing_any.append((q, missing))
    if missing_any:
        print("  FAIL: a turn does not offer the tools it cannot work without,")
        print("        or the catalogue names tools it does not offer.")
        return 1
    print("  PASS: core + discovery tools are offered on every turn, so the")
    print("        catalogue's own instructions are actionable and a turn is")
    print("        never left without the means to work or to look one up.")
    return 0



def check_every_phrasing_reaches_its_skill(enabled: dict) -> int:
    """No phrasing may STOP reaching its skill, measured against a baseline.

    The reachability loop in `main()` `break`s on the first phrasing that
    works, so a skill is recorded reachable and its remaining phrasings are
    never tried. That hid a real regression: deleting the browser intent entry
    entirely, `main()` still PASSED, because "open the website example.com"
    reaches `browser_navigate` anyway (lexical scoring matches "website" in the
    tool's own description) while "go to google.com" silently stopped reaching
    it. One phrasing surviving is enough for that loop; it is not enough to
    notice the loss.

    Why a baseline rather than "all must pass": some listed phrasings never
    worked. 17 of 234 were already failing when this check was written
    ("scrape the article" -> browser_extract, "what is my name" -> memory_get,
    and others), each covered by a second phrasing for the same skill. Demanding
    all of them pass would fail on arrival and be muted, which is worse than
    absent - so the rule is: whatever worked before must keep working. The
    known-broken ones are listed here so a NEW loss is unmistakable, and
    removing a name from this set is how one gets fixed on purpose.
    """
    # Phrasings that did not route when this check was added. Recorded, not
    # hidden: each skill still reaches via its other phrasing, so none of these
    # is a reachability failure - they are gaps in the table, pinned so that a
    # change which fixes one is noticed too (it will read as a NEW pass).
    KNOWN_BROKEN: set[tuple[str, str]] = {
        ("delete_file", "remove temp.txt"),
        ("copy_file", "make a duplicate of it"),
        ("browser_extract", "scrape the article"),
        ("calendar_list", "my schedule this week"),
        ("calendar_delete", "remove that appointment"),
        ("memory_get", "what is my name"),
        ("memory_link", "connect that to this"),
        ("sop_lookup", "how do we usually do this"),
        ("sop_list", "what runbooks do we have"),
        ("self_read", "show me your own code"),
        ("self_propose", "modify yourself"),
        ("swarm_delegate", "have the reviewer check this"),
        ("swarm_delegate", "ask the researcher to look into it"),
        ("task_schedule", "remind me to call mum at 5pm"),
        ("ask_user", "which file did you mean"),
        ("ask_user", "I am not sure what you mean"),
        ("ask_user", "can you clarify"),
    }

    broken: list[tuple[str, str]] = []
    fixed: list[tuple[str, str]] = []
    checked = 0
    for name, asks in PHRASINGS.items():
        if name not in enabled or name in CORE:
            continue
        for q in asks:
            checked += 1
            reaches = name in offered_for(q)
            if not reaches and (name, q) not in KNOWN_BROKEN:
                broken.append((name, q))
            elif reaches and (name, q) in KNOWN_BROKEN:
                fixed.append((name, q))

    print("=" * 74)
    print(f"PHRASING ROUTING: {checked} checked, {len(broken)} newly lost, "
          f"{len(fixed)} newly working")
    print("=" * 74)
    if fixed:
        for name, q in fixed:
            print(f"  [{name}] now reached by {q!r} — remove it from "
                  f"KNOWN_BROKEN to keep it working")
    if broken:
        for name, q in broken:
            print(f"  [{name}] no longer reached by {q!r}")
        print(f"\nFAIL: {len(broken)} phrasing(s) stopped reaching their skill")
        return 1
    if fixed:
        # Not a failure, but it means the baseline is now stale and would hide
        # a future loss of that phrasing. Say so rather than drift.
        print("PASS, but the baseline above is stale — update KNOWN_BROKEN")
        return 0
    print("PASS: no phrasing lost its route; the baseline is accurate")
    return 0


def main() -> int:
    catalogue_rc = check_catalogue_reaches_every_skill()
    print()
    discovery_rc = check_discovery_tools_always_offered()
    print()
    all_skills = {s.name: s for s in R.list_all()}
    enabled = {s.name: s for s in R.enabled_list_all()}

    print(f"registered: {len(all_skills)}   enabled: {len(enabled)}")
    off = sorted(set(all_skills) - set(enabled))
    if off:
        print(f"  DISABLED ({len(off)}): {off}")
    print()

    # Every skill needs a phrasing; one with none is unreachable by definition.
    # Market skills are installed content with their own SKILL.md triggers, so
    # they are reported separately rather than counted as a core-reachability
    # failure.
    missing = sorted(n for n in set(enabled) - set(PHRASINGS)
                     if enabled[n].category != "market")
    missing_market = sorted(n for n in set(enabled) - set(PHRASINGS)
                            if enabled[n].category == "market")
    if missing:
        print(f"!! {len(missing)} skill(s) have no realistic phrasing defined:")
        for n in missing:
            print(f"     - {n}  [{enabled[n].category}]")
        print()
    if missing_market:
        print(f"   ({len(missing_market)} market skill(s) not covered here — "
              f"separate concern, see note at the bottom)\n")

    reachable: set[str] = set()
    best: dict[str, str] = {}
    for name, asks in PHRASINGS.items():
        if name not in enabled:
            continue
        if name in CORE:
            # Always force-included, so it is reachable by construction.
            reachable.add(name)
            continue
        for q in asks:
            got = offered_for(q)
            if name in got:
                reachable.add(name)
                best[name] = q
                break

    never = sorted(set(enabled) - reachable)
    builtin_never = [n for n in never if enabled[n].category != "market"]
    market_never = [n for n in never if enabled[n].category == "market"]
    print("=" * 74)
    print(f"REACHABLE: {len(reachable)}/{len(enabled)}")
    print(f"NEVER OFFERED: {len(never)}   "
          f"(built-in {len(builtin_never)} | market {len(market_never)})")
    print("=" * 74)

    if builtin_never:
        from collections import defaultdict
        by_cat: dict[str, list[str]] = defaultdict(list)
        for n in builtin_never:
            by_cat[enabled[n].category].append(n)
        for cat in sorted(by_cat):
            print(f"\n  [{cat}] {len(by_cat[cat])} unreachable")
            for n in sorted(by_cat[cat]):
                print(f"     - {n}")

    print()
    print("=" * 74)
    print("SAMPLE: what a document ask actually offers")
    print("=" * 74)
    for q in ("make me a spreadsheet", "turn this into a pdf",
              "summarise this report", "read this word document"):
        print(f"  {q!r}\n     -> {sorted(offered_for(q) - CORE)}")

    print()
    phrasing_rc = check_every_phrasing_reaches_its_skill(enabled)

    if (builtin_never or missing or catalogue_rc or discovery_rc
            or phrasing_rc):
        print(f"\nFAIL: {len(builtin_never)} built-in skill(s) reachable-only-by-name, "
              f"{len(missing)} with no phrasing defined, "
              f"catalogue future-skill check rc={catalogue_rc}, "
              f"discovery-tool visibility check rc={discovery_rc}, "
              f"every-phrasing check rc={phrasing_rc}")
        if market_never:
            print(f"      ({len(market_never)} market skills also never offered — "
                  f"they are installed content, reported separately)")
        return 1
    print("\nPASS: every enabled built-in skill is reachable by some realistic "
          "phrasing, a newly registered skill reaches the prompt catalogue, and "
          "the discovery tools are offered on every turn")
    return 0


if __name__ == "__main__":
    sys.exit(main())
