"""Post-turn review — judgement about what is worth keeping.

Addled already records what a turn DID. `backend/sop/learn.py` keeps the route a
successful turn took, and `backend/memory/maintenance.py` re-embeds, extracts
facts and prunes on a timer. What neither does is decide whether a turn taught
anything. A turn that ran `read_file` three times and answered a trivia question
is recorded as a procedure; a turn that discovered the build needs `-s` and the
deploy script must run elevated is recorded as nothing at all, because both used
tools and succeeded.

So this asks a model, after the turn, one narrow question: should anything be
saved or updated? The shape is borrowed from Hermes, which forks its agent for
exactly this and found two things that matter here:

DEFERRED TO IDLE, NEVER INLINE. An inline review monopolises the model the next
prompt is about to need, and the next live turn cancels it - so the decode cost
is paid and the learning is lost. Addled's local provider serves one generation
at a time (`SINGLE_GENERATION_PROVIDERS`), so this is not a hypothetical: it is
the normal case. The review therefore only runs from the engine tick, when the
presence guard says the user is not mid-task.

COALESCED, NEWEST WINS. A review replays a conversation to judge it, so two
reviews of the same conversation are the same review done twice. Keeping only
the newest is dedup, not loss. An age-out keeps a stale review from being
dropped forever.

Hard bounds, all of them deliberate: one model call per review, a daily cap, a
minimum turn size so trivial exchanges are not reviewed, and writes that go
through the same store APIs the rest of Addled uses rather than touching files
directly. Everything here is best-effort and must never raise into a turn.
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict

from backend import app_paths

log = logging.getLogger("addled.review")

STATE_PATH = app_paths.MEMORY_DIR / "review_state.json"

# The longest a queued review waits before it is run, or dropped if the machine
# never goes quiet. A review of a conversation from hours ago is of little value
# - the skills it would have written are ones the next turn no longer needs.
MAX_AGE_S = 30 * 60

# How many reviews may run in a day. Each is one model call, so this is the
# budget that keeps the feature from quietly becoming a second assistant on the
# user's account. Low on purpose: the value is in the good ones, and they are
# rare.
DAILY_CAP = 40

# A turn must have done at least this much to be worth reviewing. One-line
# questions and answers are the overwhelming majority of turns and none of them
# teach anything; without a floor the cap would be spent on "what time is it".
MIN_TRACE_CHARS = 400

# Newest wins, so the queue stays small. This is the number of DISTINCT
# conversations tracked at once, not a message count.
MAX_QUEUE = 32

# The review's own reply is a decision object, not prose. Bounded so a chatty
# model cannot produce a paragraph this code then tries to JSON-parse.
_MAX_ANSWER_TOKENS = 700

_queue: "OrderedDict[str, dict]" = OrderedDict()
_running = False


def _load_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def enabled() -> bool:
    """Off unless the user turns it on.

    Default off, unlike most of Addled's features. This one spends model calls
    and writes to the skill store on its own initiative, and the first thing a
    user should be able to do about a feature like that is decline it.
    """
    try:
        from backend.config import config
        return bool(config.get("review", "enabled", default=False))
    except Exception as e:  # noqa: BLE001
        log.debug("review config unreadable: %s", e)
        return False


def _used_today(state: dict) -> int:
    """Reviews run since midnight, resetting the count on a new day."""
    today = time.strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"] = today
        state["count"] = 0
    return int(state.get("count", 0))


def queue_review(conversation: str, trace: str, message: str = "",
                 outcome: str = "") -> bool:
    """Put a finished turn up for review. Never raises, never blocks.

    Called on the tail of a turn that already produced its reply, so this must
    be cheap: it appends to a dict and returns. The model call happens later, on
    the tick, or not at all.

    Returns whether the turn was queued, which is what the checks assert on.
    """
    try:
        if not enabled():
            return False
        if len(str(trace or "")) < MIN_TRACE_CHARS:
            return False
        key = str(conversation or "").strip() or "default"
        _queue[key] = {
            "conversation": key,
            "trace": str(trace)[:20000],
            "message": str(message)[:2000],
            "outcome": str(outcome)[:4000],
            "at": time.time(),
        }
        # Newest wins. `move_to_end` makes an updated conversation the most
        # recent rather than the oldest, which is what "coalesce" means here.
        _queue.move_to_end(key)
        while len(_queue) > MAX_QUEUE:
            _queue.popitem(last=False)
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("queue_review failed: %s", e)
        return False


def pending() -> int:
    """How many conversations are waiting. For status and for the checks."""
    return len(_queue)


def _take_due(state: dict) -> dict | None:
    """Pop the oldest still-usable entry, dropping any that aged out."""
    now = time.time()
    while _queue:
        key, item = _queue.popitem(last=False)
        if now - float(item.get("at", 0)) <= MAX_AGE_S:
            return item
        log.debug("dropped a review queued %.0fs ago", now - item["at"])
    return None


def build_prompt(item: dict, skills_index: str) -> list[dict]:
    """The one question, with what is already known.

    The skills index is included so the review can UPDATE rather than duplicate:
    asked to save without being shown what exists, a model reliably writes a
    near-copy of a skill that is already there. Hermes carries its index into
    the same call for the same reason.
    """
    system = (
        "You review a finished assistant turn and decide what, if anything, "
        "should be remembered.\n\n"
        "You are NOT replying to the user. Nobody will read this as a "
        "message. Your output is a decision.\n\n"
        "Save a SKILL when the turn found a durable HOW: a procedure, a "
        "command that had to be exactly right, a pitfall that cost time, a "
        "source worth going back to. Save a FACT when the turn learned "
        "something durable ABOUT THE USER or their setup.\n\n"
        "Do not save: anything already listed below, anything specific to "
        "this one question, the user's own words restated, or a summary of "
        "what happened.\n\n"
        "Write facts as declarative statements about the world "
        "('The build needs -s on Windows'), never as instructions to "
        "yourself ('Always use -s'), because an instruction is re-read later "
        "as an order and overrides the user.\n\n"
        "Reply with ONE JSON object and nothing else:\n"
        '{"skill": {"name": "...", "description": "one line", '
        '"body": "the steps", "update_of": "existing name or null"} | null, '
        '"fact": "text" | null, "reason": "why, in a few words"}\n'
        "Both null is the normal, expected answer for most turns.\n\n"
        "Skills already available:\n" + (skills_index or "(none)")
    )
    user = (
        f"The user asked: {item.get('message') or '(unknown)'}\n\n"
        f"The turn did: {item.get('trace')}\n\n"
        f"It ended with: {item.get('outcome') or '(no reply recorded)'}"
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def parse_decision(text: str) -> dict:
    """Pull the decision object out of a model reply. Never raises.

    Models wrap JSON in prose and code fences, so this finds the balance of the
    first object rather than trusting the whole reply to be JSON. A reply with
    no object at all is `{}` - which reads downstream as "nothing to save",
    the safe direction.
    """
    raw = str(text or "")
    start = raw.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(raw[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = raw.find("{", start + 1)
    return {}


def _skills_index() -> str:
    """Name and one line per skill — what already exists, for update-not-duplicate."""
    try:
        from backend.skills.registry import skill_registry
        lines = []
        for s in skill_registry.enabled_list_all():
            if s.category == "meta":
                continue
            lines.append(f"- {s.name}: {s.description}")
        return "\n".join(lines[:200])
    except Exception as e:  # noqa: BLE001
        log.debug("skills index unavailable: %s", e)
        return ""


def apply_decision(decision: dict) -> dict:
    """Write what the review decided, through the normal store APIs.

    Returns a small record of what was written, for logging and the checks.
    Nothing here touches a file directly: a fact goes through the facts store
    and a skill through the registry, so everything the rest of Addled does to
    those stores - dedup, linking, expiry - still applies to it.
    """
    wrote: dict = {"fact": False, "skill": ""}
    if not isinstance(decision, dict):
        return wrote

    fact = decision.get("fact")
    if isinstance(fact, str) and fact.strip():
        try:
            from backend.memory import facts as facts_mod
            add = getattr(facts_mod, "add_fact", None)
            if callable(add):
                add(fact.strip())
                wrote["fact"] = True
                log.info("Review saved a fact: %s", fact.strip()[:120])
        except Exception as e:  # noqa: BLE001
            log.debug("review could not save a fact: %s", e)

    skill = decision.get("skill")
    if isinstance(skill, dict) and (skill.get("name") or "").strip():
        try:
            wrote["skill"] = _write_skill(skill)
        except Exception as e:  # noqa: BLE001
            log.debug("review could not save a skill: %s", e)
    return wrote


def _write_skill(skill: dict) -> str:
    """Store a skill the review wrote, as guidance rather than code.

    Deliberately NOT the forge. The forge generates and runs Python, which is
    the right thing for a capability and the wrong thing for a procedure: this
    text is instructions, and it must not be able to execute anything. It lands
    in the registry with a body, which is what `skill_view` reads.
    """
    from backend.skills.registry import SkillDefinition, skill_registry

    name = "".join(ch if (ch.isalnum() or ch == "_") else "_"
                   for ch in str(skill.get("name", "")).strip().lower())
    name = name.strip("_")[:60]
    if not name:
        return ""
    body = str(skill.get("body") or "").strip()
    desc = " ".join(str(skill.get("description") or "").split())[:200]

    existing = skill_registry.get(name)
    if existing is not None:
        # Update in place, keeping the handler. A learned skill has no handler
        # worth keeping, but a name collision with a built-in does, and
        # overwriting a built-in's behaviour from a review would be dangerous.
        if existing.category != "learned":
            log.info("Review declined to overwrite the %s skill %r",
                     existing.category, name)
            return ""
        existing.body = body
        existing.description = desc or existing.description
        return name

    async def _guidance(params: dict) -> dict:
        """A learned skill answers with its own instructions."""
        return {"success": True, "instructions": body,
                "note": "Follow these instructions."}

    skill_registry.register(SkillDefinition(
        name=name,
        description=desc or f"Learned: {name}",
        parameters={"type": "object", "properties": {}},
        handler=_guidance,
        category="learned",
        body=body,
    ))
    log.info("Review learned a skill: %s", name)
    return name


async def run_review(item: dict) -> dict:
    """One review: ask, parse, apply. Never raises.

    The model is resolved lazily and the whole thing degrades to a no-op if no
    provider is available, because a review is never worth an error in a log the
    user did not ask for.
    """
    global _running
    if _running:
        return {"skipped": "already running"}
    _running = True
    try:
        from backend.providers.registry import get_provider
        from backend.providers.router import resolve_model

        provider = get_provider()
        if provider is None:
            return {"skipped": "no provider"}

        # Prefer the cheap role. On a machine where `utility` is unset this
        # resolves to the provider default, which is the same model chat uses -
        # more expensive than intended, but still correct, and never a failure.
        model = None
        try:
            model = resolve_model(getattr(provider, "provider_id", ""),
                                  "utility")
        except Exception as e:  # noqa: BLE001
            log.debug("no utility model for the review: %s", e)

        messages = build_prompt(item, _skills_index())
        result = await provider.chat(messages, model=model,
                                     max_tokens=_MAX_ANSWER_TOKENS,
                                     temperature=0.2)
        if not getattr(result, "ok", False):
            return {"error": getattr(result, "error", "provider failed")}

        decision = parse_decision(getattr(result, "response", ""))
        if not decision:
            return {"decision": None, "note": "the review had nothing to say"}
        wrote = apply_decision(decision)
        return {"decision": decision, "wrote": wrote,
                "reason": decision.get("reason", "")}
    except Exception as e:  # noqa: BLE001
        log.warning("Review failed: %s", e)
        return {"error": str(e)}
    finally:
        _running = False


async def run_due_reviews(max_reviews: int = 1) -> dict:
    """Drain up to `max_reviews` due entries. The engine tick calls this.

    Single-flight and cheap when there is nothing to do, so it is safe to call
    on every tick. `max_reviews` defaults to one: this shares a model with the
    conversation, and a queue that drains in one tick would be felt as a stall.
    """
    if not enabled():
        return {"skipped": "review is turned off"}
    if not _queue:
        return {"skipped": "nothing queued"}

    state = _load_state()
    used = _used_today(state)
    if used >= DAILY_CAP:
        return {"skipped": f"daily cap reached ({used})"}

    done = []
    for _ in range(max(0, int(max_reviews))):
        item = _take_due(state)
        if item is None:
            break
        outcome = await run_review(item)
        done.append({"conversation": item["conversation"], **outcome})
        used += 1
        state["count"] = used
        state["last"] = time.time()
        _save_state(state)

    if not done:
        return {"skipped": "nothing due"}
    return {"reviews": done, "count": len(done)}


def _reset_for_tests() -> None:
    """Clear the in-memory queue. Used by the check, never by the app."""
    _queue.clear()
