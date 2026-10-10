"""Verify that a moment turns into the right emotion.

The character's face is driven by three things now: the mood engine's slow
background feeling, an emotion the model names in its reply, and an emotion
derived from what the observer noticed. This suite covers the two decisions
made in our own code — reading a tag out of a reply, and classifying a reply
that has no tag — because those are the ones that can be wrong silently.

The rules enforced here are the ones that were actually broken while this was
being written:

  * A reply of nothing but a tag became "I couldn't process that request."
    because the tag was stripped after the empty-reply check rather than
    before. The decode now runs first.

  * "Are you sure?" classified as CONFUSION, on the strength of its question
    mark alone. Punctuation is not evidence; a word is.

  * "WHAT WERE YOU THINKING" classified as NEUTRAL. Shouting was scored below
    one word's weight, so a deliberate shout was discarded as noise.

  * A bracket that is not an emotion — a markdown link, "[see the docs]" — was
    read as an emotion and deleted from the reply.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_trigger.py
"""

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.character import trigger  # noqa: E402
from backend.character.emotions import EMOTIONS, emotion_names  # noqa: E402

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {label}")
    else:
        suffix = f"  <- {detail}" if detail else ""
        print(f"  FAIL {label}{suffix}")
        fails.append(label)


def main() -> int:
    print("the menu offers only drawable emotions")

    # The menu is what the model is allowed to write. Advertising a word the
    # avatar cannot draw means the model picks it, the tag never matches, and
    # the tag is then shown to the user verbatim as if it were punctuation.
    menu = trigger.emotion_menu()
    words = [w.strip("[]") for w in menu.split()]
    check("the menu is not empty", bool(words), menu)
    check("every word in the menu is a real emotion",
          all(w in EMOTIONS for w in words),
          str([w for w in words if w not in EMOTIONS]))
    check("the menu covers every emotion",
          set(words) == set(emotion_names()),
          f"missing {set(emotion_names()) - set(words)}")

    print("reading the tag the model wrote")

    # The tag is at the front in practice, but nothing guarantees that, and a
    # model that appends one must still be understood.
    got, text = trigger.parse_tag("[joy] Thanks, that worked!")
    check("a leading tag is read", got == "joy", got)
    check("a leading tag is removed from the reply",
          text == "Thanks, that worked!", repr(text))

    got, text = trigger.parse_tag("That's a shame [sadness].")
    check("a tag elsewhere in the reply is read", got == "sadness", got)
    check("a mid-reply tag is removed", text == "That's a shame .", repr(text))

    got, text = trigger.parse_tag("[JOY] Loud.")
    check("the tag is matched case-insensitively", got == "joy", got)

    # A spelling variant the emotions table already knows must land on the
    # canonical name, or the avatar is handed a word it cannot look up.
    got, _ = trigger.parse_tag("[sad] Oh.")
    check("an aliased tag resolves to its canonical name", got == "sadness",
          got)

    # A reply with no tag is neutral, and — critically — unchanged. Cleaning
    # text that had nothing to clean is how a reply gets silently edited.
    raw = "Just a plain sentence."
    got, text = trigger.parse_tag(raw)
    check("no tag means neutral", got == "neutral", got)
    check("a reply with no tag is returned untouched", text == raw, repr(text))

    # Square brackets are ordinary punctuation. Reading "[see the docs]" as an
    # emotion DELETES it, so the reply loses a word and the user never knows.
    raw = "See [the docs] for that."
    got, text = trigger.parse_tag(raw)
    check("a bracket that is not an emotion is not read as one",
          got == "neutral", got)
    check("a bracket that is not an emotion is left in the reply",
          text == raw, repr(text))

    # A markdown link is the common case of the above and is worth its own
    # case, because a reply containing a link is exactly when this would bite.
    raw = "Here: [the guide](https://example.com/x)."
    got, text = trigger.parse_tag(raw)
    check("a markdown link survives", "the guide" in text and got == "neutral",
          repr(text))

    # A single-word bracket is the case that actually bites, and the multi-word
    # ones above do NOT cover it: the tag pattern matches one word, so
    # "[see the docs]" can never match it and always passes, while "[docs]"
    # looks like a perfect tag. Written only against the multi-word case, this
    # suite reported everything fine while a reply saying "See [docs] for that."
    # was silently reduced to "See for that." — the word gone, the user never
    # told.
    for raw, must_keep in (
        ("See [docs] for that.", "docs"),
        ("Use [sudo] here.", "sudo"),
        ("Run [pytest] first.", "pytest"),
        ("The [config] file.", "config"),
        ("Ask [claude] about it.", "claude"),
    ):
        got, text = trigger.parse_tag(raw)
        check(f"an ordinary bracketed word survives: {must_keep!r}",
              must_keep in text and got == "neutral",
              f"got {got!r} / {text!r}")

    # The same through `decide`, since that is what the chat pipeline calls.
    got, text = trigger.decide("See [docs] for that.")
    check("decide() keeps an ordinary bracketed word", "docs" in text,
          repr(text))

    # An empty or missing reply must not raise: this runs on every chat turn.
    for empty in ("", None):
        try:
            got, text = trigger.parse_tag(empty)
            check(f"an empty reply ({empty!r}) is handled", got == "neutral",
                  got)
        except Exception as e:  # noqa: BLE001
            check(f"an empty reply ({empty!r}) is handled", False, str(e))

    print("classifying a reply that has no tag")

    # These are the readings the character must get right. Each is a sentence
    # a person would plausibly type, with the emotion it plainly calls for.
    cases = (
        ("This is brilliant, thank you!", "joy"),
        ("I am so worried about this", "fear"),
        ("Sorry about that.", "sadness"),
        ("Are you sure?", "concern"),
        ("WHAT WERE YOU THINKING", "anger"),
        ("I am confused about the config", "confusion"),
        ("Good job", "happy"),
        ("Everything looks fine.", "neutral"),
        ("just a normal sentence here", "neutral"),
    )
    for text, want in cases:
        got = trigger.classify_text(text)[0]
        check(f"{text!r} reads as {want}", got == want, f"got {got}")

    # Punctuation alone is not a statement of feeling. "?" adding confusion to
    # any question meant an ordinary question put an expression on the face,
    # and questions are extremely common in a chat with an assistant.
    for text in ("Is the config ready?", "Did that work?", "Ready!", "Really!"):
        got = trigger.classify_text(text)[0]
        check(f"{text!r} gets no emotion from its punctuation alone",
              got == "neutral", f"got {got}")

    # A word plus punctuation must still work — the rule above must not have
    # been implemented by ignoring punctuation entirely.
    got = trigger.classify_text("I am confused?!")[0]
    check("a word plus punctuation still reads", got == "confusion", got)

    # Shouting must reach the bar on its own. Scored below a word's weight it
    # was thrown away, which is the opposite of what shouting means.
    got = trigger.classify_text("THIS IS RIDICULOUS")[0]
    check("a shout reads as anger", got == "anger", got)

    # ...but a short all-caps word is not a shout. "OK" and "ASAP" are ordinary.
    for text in ("OK", "ASAP", "Hi"):
        got = trigger.classify_text(text)[0]
        check(f"{text!r} is not treated as shouting", got == "neutral", got)

    print("the classifier cannot invent an emotion")

    # The result is handed to the 30fps paint loop, so it must always be a name
    # the avatar can look up. A classifier that returns "grumpyish" would crash
    # or silently blank the face.
    probe = [
        "", "   ", "hello", "?!?!?!", "....", "😂", "😭😭😭", "GGGGGGGG",
        "a" * 200, "[]", "[not an emotion]", "100% done!!!",
    ]
    for text in probe:
        try:
            name, confidence, triggers = trigger.classify_text(text)
            # `neutral` is a real answer, not a failure to find one: it is the
            # documented result for "nothing here argues for an expression".
            # What must never happen is a name the avatar cannot look up.
            ok = (name == "neutral" or name in EMOTIONS) and 0.0 <= confidence <= 1.0
            check(f"classifying {text[:20]!r} gives a drawable emotion", ok,
                  f"{name!r} conf={confidence}")
        except Exception as e:  # noqa: BLE001
            check(f"classifying {text[:20]!r} does not raise", False, str(e))

    # The triggers list is the whole reason this is debuggable rather than
    # magic: a wrong reading has to be explainable from its output.
    name, confidence, triggers = trigger.classify_text("Thanks so much!!")
    check("a confident reading names its triggers", bool(triggers),
          str(triggers))

    print("deciding, given a whole reply")

    # The tag wins. The model read the conversation; the classifier read one
    # string, so when they disagree the model is the better authority.
    got, text = trigger.decide("[joy] Ugh, this is awful.")
    check("the tag beats the classifier", got == "joy", got)
    check("the tag is stripped from the decided reply", "[joy]" not in text,
          repr(text))

    # With no tag, the classifier is consulted — but only when confident. A
    # weak guess would put an expression on a neutral turn.
    got, _ = trigger.decide("I am so worried about this")
    check("a clear reply is classified when there is no tag", got == "fear",
          got)
    got, _ = trigger.decide("Everything looks fine.")
    check("an ordinary reply stays neutral", got == "neutral", got)

    # The classifier must be off entirely when a caller says so, which is how
    # a surface that wants tags only avoids the guess.
    got, _ = trigger.decide("I am so worried about this",
                            allow_classifier=False)
    check("the classifier is skipped when disabled", got == "neutral", got)

    # Any reply must survive the decision unchanged in type and without raising.
    for text in ("", "hi", "[anger]", "😀" * 40):
        try:
            got, cleaned = trigger.decide(text)
            ok = isinstance(got, str) and isinstance(cleaned, str)
            check(f"deciding {text[:16]!r} returns strings", ok,
                  f"{got!r} {cleaned!r}")
        except Exception as e:  # noqa: BLE001
            check(f"deciding {text[:16]!r} does not raise", False, str(e))

    if fails:
        print(f"FAIL: {len(fails)}: {fails}")
        return 1
    print("PASS: a moment turns into the right emotion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
