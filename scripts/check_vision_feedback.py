"""Vision must report WHY it failed, and must not fall silent while it works.

Two live defects, both found by a turn that answered "the visual model is
unavailable" for an image that was fine:

1. NO REASON RECORDED. `_analyze_attachments` did `except Exception:
   desc = None` on both routes, and its caller logged the whole block at DEBUG.
   At normal log levels a genuine vision failure therefore produced NO line at
   all, so a reply claiming vision was unavailable could not be checked against
   anything - which is how a model-invented excuse went unnoticed. Each route
   must now name its own failure at WARNING.

2. NO FEEDBACK WHILE WORKING. Local vision costs ~8s of CPU per image plus a
   ~16s one-time model load (measured, not assumed), and transcription is
   seconds more. The composer simply froze. `notify` now reports the slow step.

Speed was measured and deliberately NOT "optimised": lowering max_new_tokens
from 512 to 192 changed nothing (8.3s vs 8.4s) because the model stops at its
own EOS after ~55 words, while 64 tokens truncated the caption. Capping it
would cost quality for no gain.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_vision_feedback.py
"""

import os
import re
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

SERVER = os.path.join(ROOT, "backend", "ws_server.py")
HF = os.path.join(ROOT, "backend", "providers", "hf_vision.py")
CHAT_PAGE = os.path.join(ROOT, "dashboard", "src", "app", "chat", "page.tsx")

fails: list[str] = []

def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        fails.append(f"{label}: {detail}" if detail else label)

def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()

def main() -> int:
    for path in (SERVER, HF):
        if not os.path.isfile(path):
            print(f"missing file: {path}")
            return 2
    server = read(SERVER)

    # -- 1. the failure reason must be recorded ---------------------------
    image_branch = server[server.index('if kind == "image" and data:'):]
    image_branch = image_branch[:image_branch.index('elif kind == "text"')]

    check("the image branch collects reasons instead of discarding them",
          "why = []" in image_branch and "why.append" in image_branch,
          "bare `except: desc = None` loses the only record of what went wrong")
    # Every route must record its own reason. Counting alone is not enough -
    # removing ONE append still leaves several - so each is matched where it
    # belongs, with the error text it carries.
    provider_route = image_branch[
        image_branch.index("if getattr(provider"):image_branch.index("if not desc")]
    florence_route = image_branch[
        image_branch.index("if not desc"):image_branch.index("if desc:")]

    check("the provider's ok=False reason is recorded",
          re.search(r"why\.append\(f\"provider", provider_route) is not None,
          "an ok=False with no recorded reason is the original silent failure")
    check("the provider's own error text is included",
          "res.error" in provider_route,
          "logging only 'the provider failed' hides what the provider said")
    check("the provider's raised exception is recorded",
          re.search(r"why\.append\(f\"provider raised \{type\(e\)\.__name__\}", provider_route),
          "an unnamed exception cannot be diagnosed")
    check("a provider without vision says so",
          "does not support vision" in provider_route)
    check("the Florence-2 route records its error",
          re.search(r"why\.append\(f\"Florence-2", florence_route) is not None)
    check("Florence-2's own error text is included",
          "res.error" in florence_route)
    check("a non-vision provider states that fact",
          "does not support vision" in image_branch,
          "'provider unavailable' is not the same as 'provider has no vision'")
    check("the failure is logged at WARNING, not debug",
          re.search(r'log\.warning\(\s*"Could not analyse attached image', server) is not None)
    check("the warning includes every collected reason",
          "'; '.join(why)" in server or '"; ".join(why)' in server)
    check("the provider exception is caught with a name",
          "provider raised {type(e).__name__}" in image_branch.lower()
          or "provider raised" in image_branch.lower(),
          "an unnamed exception cannot be diagnosed")

    # -- 1b. the wrapper must not hide failures at DEBUG -------------------
    check("the attachment-analysis wrapper logs at WARNING",
          re.search(r'log\.warning\("Attachment analysis failed', server) is not None,
          "at DEBUG a real failure left no trace at default verbosity")
    check("the code-attachment wrapper logs at WARNING",
          re.search(r'log\.warning\("Could not analyse code attachments', server) is not None)

    # -- 2. slow steps must announce themselves ---------------------------
    check("_analyze_attachments accepts a notify callback",
          re.search(r"def _analyze_attachments\([^)]*notify", server, re.S) is not None)
    check("the image step reports before the wait",
          "await notify(" in image_branch and "Looking at" in image_branch)
    check("the audio step reports before the wait",
          "Listening to" in server and "await notify(" in server)
    check("the caller supplies a notify function",
          re.search(r"_analyze_attachments\(\s*provider, attachments,"
                    r" vision_model=_vision_model,\s*notify=_say", server) is not None,
          "an unsupplied callback means no feedback ever reaches the user")
    check("the notice is broadcast on a named channel",
          'broadcast("chat.activity"' in server)
    check("a broadcast failure cannot break the turn",
          re.search(r"async def _say.*?except Exception", server, re.S) is not None)

    # -- 2b. max_new_tokens must NOT be cut to chase speed ----------------
    # Measured: 512 -> 8.3s / 55 words; 192 -> 8.4s / 55 words; 64 -> 8.2s but
    # truncated. Lowering it buys nothing and risks a cut-off description.
    hf = read(HF)
    cap = re.search(r"max_new_tokens:\s*int\s*=\s*(\d+)", hf)
    check("the caption token cap is still generous",
          cap is not None and int(cap.group(1)) >= 256,
          f"found {cap.group(1) if cap else '?'} — under ~128 the caption truncates")

    # -- 3. the UI shows the feedback, and clears it ----------------------
    if os.path.isfile(CHAT_PAGE):
        page = read(CHAT_PAGE)
        check("the chat page subscribes to chat.activity",
              "onNotification('chat.activity'" in page)
        check("the notice is rendered",
              "{activity &&" in page)
        check("it is announced to assistive tech",
              'aria-live="polite"' in page and 'role="status"' in page)
        check("it is cleared when the turn ends",
              re.search(r"finally\s*\{[^}]*setActivity\(''\)", page, re.S) is not None,
              "a notice that outlives its turn is a lie about what is happening")
    else:
        check("the chat page is present", False, CHAT_PAGE)

    if fails:
        print(f"FAIL: {len(fails)} problem(s)")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: vision failures name their cause, and slow steps announce "
          "themselves on both sides")
    return 0

if __name__ == "__main__":
    sys.exit(main())
