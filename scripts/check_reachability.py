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

import sys

sys.path.insert(0, "E:/Kunoir/Codeground/Clicky/Addled")

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
    "task_schedule": ["remind me to call mum at 5pm", "schedule a task for tomorrow"],
    "task_list": ["what reminders do I have", "list my scheduled tasks"],
    "task_cancel": ["cancel that reminder"],
    # The awkward one: `ask_user` is called when the model is UNSURE, so there
    # is no user phrasing that asks for it. These are the situations where the
    # model needs it, which is the closest honest equivalent — the check wants
    # to know it can be offered, not that a user would request it by name.
    "ask_user": ["which file did you mean", "I am not sure what you mean",
                 "can you clarify", "ask me a question"],
}


def offered_for(query: str, max_tools: int = 10) -> set[str]:
    return R.filter_for_query(query, max_tools=max_tools)


def main() -> int:
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

    if builtin_never or missing:
        print(f"\nFAIL: {len(builtin_never)} built-in skill(s) reachable-only-by-name, "
              f"{len(missing)} with no phrasing defined")
        if market_never:
            print(f"      ({len(market_never)} market skills also never offered — "
                  f"they are installed content, reported separately)")
        return 1
    print("\nPASS: every enabled built-in skill is reachable by some realistic phrasing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
