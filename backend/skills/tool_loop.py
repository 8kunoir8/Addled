"""
Provider-agnostic tool-use loop with auto-forging.

Wraps any AI provider to enable function calling via the Skill Registry.
When a skill is missing, the Skill Forge auto-discovers and creates it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from backend.providers import budget
from backend.skills.registry import skill_registry, SkillResult

log = logging.getLogger("addled.tool_loop")

NATIVE_TOOL_PROVIDERS = {"openai", "deepseek", "gemini", "openrouter"}
MAX_TOOL_ROUNDS = 8

def current_turn() -> dict:
    """Which conversation the running turn belongs to.

    A thin read of `backend.chat_context`, which already carries the origin for
    the approval gate. `ask_user` needs the same fact for the same reason — the
    card has to reach the chat that asked, and an answer must not arrive from
    somewhere else — so it reads the existing ContextVar rather than keeping a
    second copy that could disagree with the first.
    """
    try:
        from backend import chat_context
        return chat_context.origin()
    except Exception as e:  # noqa: BLE001
        log.debug("could not read the turn origin: %s", e)
        return {}



async def execute_skill(name: str, params: dict, provider=None) -> dict:
    """Execute a skill by name, recording usage/failure telemetry."""
    result = await _execute_skill_inner(name, params, provider)
    try:
        from backend.skills.telemetry import record
        record(name, bool(result.get("success")))
    except Exception:
        pass
    return result


def forge_target_provider(provider=None):
    """The provider a forge should write code with.

    Forging is code generation, and the model that failed to answer the
    question is rarely the one that should write the tool which answers it. A
    local 4B model asked to emit a working async function is the weakest link
    in the chain, so when the chat provider is a local one and something better
    is configured, the forge borrows it. The chat provider is still used when
    no alternative exists — a weak forge beats no forge.
    """
    try:
        from backend.providers.registry import get_provider
        from backend.providers import selector
    except Exception:
        return provider

    weak = {"local", "ollama", "lmstudio", "huggingface"}
    current_id = str(getattr(provider, "provider_id", "") or "")

    def _is_local(pid: str) -> bool:
        if pid in weak:
            return True
        try:
            from backend.config import config
            return bool(config.get("providers", "builtin", pid,
                                   "local", default=False))
        except Exception:
            return False

    if provider is not None and not _is_local(current_id):
        return provider

    try:
        chosen = selector.resolve_default_provider()
    except Exception:
        return provider
    if not chosen or _is_local(str(chosen)):
        return provider  # nothing better on offer — do not downgrade
    try:
        better = get_provider(chosen)
    except Exception:
        return provider
    if better is None or better is provider:
        return provider
    log.info("Forging with '%s' instead of the local chat provider", chosen)
    return better


async def _ask_to_acquire(kind: str, what: str, detail: str,
                          options: list[str]) -> dict:
    """Ask before changing what Addled can do.

    Installing a market skill and forging new code both fetch or generate
    executable code that then runs on the user's machine. That used to happen as
    a side effect of answering a question: `search_and_install` installed inside
    the search and the loop set `"market": True` / `"installed_skill"`, which
    nothing read. Nobody was asked and nobody was told.

    Returns the question result. `requires_answer` is set on success, which is
    the marker the loop stops on — the turn ends and the user's answer arrives
    as the next message.

    On an UNATTENDED source (a scheduled task, a swarm desk) `pending.ask`
    refuses, and that refusal is returned as-is rather than raised. The caller
    must treat it as "no consent" — never as "proceed". Installing code on the
    say-so of nobody would be the same bug in a different costume.
    """
    try:
        from backend.questions import pending
        ctx = current_turn()
        question = (f"I can {kind} for this: {what}. {detail} "
                    f"Shall I go ahead?")
        result = pending.ask(
            question,
            options=options,
            source=ctx.get("source") or "",
            conversation=ctx.get("conversation") or "",
            context=f"{kind.title()} changes what Addled can run on this machine.",
        )
        if result.get("success"):
            result["requires_answer"] = True
        return result
    except Exception as e:  # noqa: BLE001
        log.debug("could not ask before acquiring: %s", e)
        return {"success": False, "error": f"could not ask first: {e}"}


def _consented(question_result: dict) -> bool:
    """Whether the user has said yes to acquiring something.

    Read from THIS turn's own request text, which the pipeline publishes to
    `_forge.request` for every turn. That is the user's words, and a new turn
    carries them, so an answer typed after the question is visible here without
    a flag that a fresh turn would not see.

    Wording is a blunt instrument. The alternatives are worse: a module-level
    flag would leak between conversations and survive a restart, and trusting
    the model to withhold the call on its own is what produced the silent
    install this exists to stop. A refusal always wins when both appear ("no,
    don't install it"), because the safe reading of an ambiguous answer is the
    one that changes nothing.
    """
    try:
        from backend.config import config
        low = str(config.get("_forge", "request", default="") or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if not low:
        return False
    if not _ACQUIRE_QUESTION_RE.search(low):
        # No question was asked this turn, so there is no answer to read. Being
        # asked is what makes a yes meaningful.
        return False
    no = ("no", "don't", "dont", "skip", "cancel", "stop", "never", "not now")
    if any(re.search(rf"\b{w}\b", low) for w in no):
        return False
    yes = ("yes", "go ahead", "install it", "do it", "ok", "okay", "sure",
           "proceed", "forge it", "add it", "install")
    return any(re.search(rf"\b{w}\b", low) for w in yes)

# The question this module asks, recognised in the answer. Matched on a phrase
# that only the acquisition question uses, so an ordinary "yes" to something
# else cannot be mistaken for consent to install code.
_ACQUIRE_QUESTION_RE = re.compile(r"shall i go ahead|i can install|i can forge")


async def _try_cli_tool(name: str, params: dict) -> dict | None:
    """Run the user's own CLI tool for this call, if one matches.

    Returns None when nothing matches, so the caller continues down the chain.
    A match that FAILS still returns a result: the user asked for a capability
    and their tool is the answer, so a broken tool is reported rather than
    silently falling through to downloading something else.

    `prefer_over_mcp=False` skips this step entirely, which is the switch that
    restores the order Addled had before CLI tools existed.
    """
    try:
        from backend.config import config
        if not config.get("cli_tools", "enabled", default=True):
            return None
        if not config.get("cli_tools", "prefer_over_mcp", default=True):
            return None
    except Exception:  # noqa: BLE001
        return None

    try:
        from backend.cli_tools.registry import cli_tools

        request = ""
        try:
            from backend.config import config as _cfg
            request = str(_cfg.get("_forge", "request", default="") or "")
        except Exception:  # noqa: BLE001
            pass

        tool = cli_tools.find_match(name, request)
        if tool is None:
            return None

        log.info("CLI tool '%s' answers the call for '%s'", tool.slug, name)
        try:
            result = await skill_registry.execute(tool.name, params)
        except Exception as e:  # noqa: BLE001
            # The tool MATCHED, so it owns this call even when it breaks. It was
            # the user's choice, and quietly running something downloaded
            # instead would do the opposite of what they asked for — the whole
            # point of preferring a tool they reviewed. The failure is reported.
            log.warning("CLI tool '%s' failed: %s", tool.slug, e)
            return {
                "success": False,
                "data": {},
                "error": f"the tool '{tool.name}' failed: {e}",
                "forged": False,
                "cli_tool": tool.slug,
            }
        return {
            "success": result.success,
            "data": result.data,
            "error": result.error,
            "forged": False,
            "cli_tool": tool.slug,
        }
    except Exception as e:  # noqa: BLE001
        # The LOOKUP itself failed — the registry is unavailable, nothing was
        # matched, no tool was chosen. Falling through is correct here: there is
        # no user choice to honour, and the chain continues to the market.
        log.debug("CLI tool lookup failed for '%s': %s", name, e)
        return None


def _buildable_capability(name: str, params: dict) -> str:
    """A short phrase describing what is missing, for the Build question.

    The user's own words when the turn published them, because "build me a tool
    to fetch the prices off this page" is a usable brief and "fetch_prices" is
    not. The call name is the fallback.
    """
    try:
        from backend.config import config
        request = str(config.get("_forge", "request", default="") or "").strip()
    except Exception:  # noqa: BLE001
        request = ""
    if request:
        return request[:300]
    readable = str(name or "").replace("_", " ").strip()
    return readable or "this capability"


def _build_offer_enabled() -> bool:
    """Whether the "shall I build one?" question is the active gate.

    One function rather than a repeated config read, because two places have to
    agree on it: `_offer_to_build` raises the question, and `_execute_skill_inner`
    skips the forge's own consent prompt while it is on. If they disagreed, the
    user would be asked twice or not at all.
    """
    try:
        from backend.config import config
        return (bool(config.get("cli_tools", "enabled", default=True))
                and bool(config.get("cli_tools", "ask_before_build",
                                    default=True)))
    except Exception:  # noqa: BLE001
        return False



async def _offer_to_build(name: str, params: dict) -> dict | None:
    """Ask whether to build a tool, before looking for one to download.

    Returns None when the question should not be asked — the feature is off, no
    provider could write the code, or the answer is already on record — so the
    chain continues to the market.

    Returns a result dict when the question was raised. That ENDS the turn the
    same way the acquisition questions do: the answer arrives as the next
    message, so waiting in-band would lose the race with the socket timeout.
    """
    if not _build_offer_enabled():
        return None

    # A question already answered this turn must not be asked again; without
    # this the model loops, asking the same thing every round until the tool
    # cap. `_consented` reads the answer for the acquisition questions and
    # applies here for the same reason.
    try:
        if _consented(None):
            return None
    except Exception:  # noqa: BLE001
        return None

    try:
        capability = _buildable_capability(name, params)
        asked = await _ask_to_acquire(
            "build a small tool for this",
            capability,
            "It would be written here, shown to you to review on the "
            "Settings -> CLI Tools page, and used from chat, code and your "
            "swarm agents. Nothing is downloaded, and nothing runs until "
            "you approve it.",
            ["Build it", "Search for a tool instead"])
    except Exception as e:  # noqa: BLE001
        log.debug("build offer failed: %s", e)
        return None

    if not asked or not asked.get("question_id"):
        return None
    return {
        "success": False,
        "data": {"requires_answer": True,
                 "capability": capability,
                 "suggested_slug": _slug_hint(name or capability),
                 **{k: v for k, v in asked.items() if k == "question_id"}},
        "error": asked.get("error") or
                 ("I don't have a tool for this yet. I can build one — ask the "
                  "user whether to build it or look for an existing tool, and "
                  "wait for their answer. Do not install anything first."),
        "forged": False,
    }


def _slug_hint(text: str) -> str:
    """A filesystem-safe suggestion for the tool's name."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug[:40] or "new-tool"



async def _execute_skill_inner(name: str, params: dict, provider=None) -> dict:
    """
    Execute a skill by name. If not found, try the market first, then the forge.
    Returns {"success": bool, "data": dict, "forged": bool, ...}
    """
    skill = skill_registry.get(name)
    if skill:
        result = await skill_registry.execute(name, params)
        return {
            "success": result.success,
            "data": result.data,
            "error": result.error,
            "forged": False,
        }

    # Not a registered skill — but the user may have BUILT one for this, and a
    # tool they wrote and reviewed beats anything fetched. Tried before the
    # market and before the forge, which is the whole point of the feature:
    # asking for a capability should reach the user's own tool first.
    #
    # `find_match` also sees the turn's own request text, because the call name
    # is often generic while the request carries the intent.
    cli_result = await _try_cli_tool(name, params)
    if cli_result is not None:
        return cli_result

    # Nothing built for this. Before downloading anything, offer to build it —
    # the user can only ask for a tool if they know the option exists, and a
    # market install runs code fetched from the internet where a tool built here
    # is code they will review on the Settings page first.
    #
    # Asked at most once per capability: the question is raised only while no
    # consent is on record, and the answer arrives as the next turn. Declining
    # skips straight to the market below, because a refusal must never leave the
    # capability unmet.
    offer = await _offer_to_build(name, params)
    if offer is not None:
        return offer

    # Skill not found — find a market match, ASK, and install only on a yes.
    log.info("Skill '%s' not found — looking for it in the market", name)
    try:
        from backend.config import config
        if config.get("skills", "market_search", default=True):
            from backend.skills.market_search import find_match, install_match
            candidate = await find_match(
                name,
                float(config.get("skills", "market_sim_threshold",
                                 default=0.45)))
            if candidate:
                if not _consented(None):
                    # Nothing to install yet: ask, and let the answer come back
                    # as the next turn. Returning here ENDS the acquisition —
                    # the retry carries the consent.
                    asked = await _ask_to_acquire(
                        "install a market skill",
                        str(candidate.get("name") or name),
                        str(candidate.get("description") or "")[:200]
                        + f" (from github.com/{candidate.get('repo')})",
                        ["Install it", "Skip this"])
                    return {
                        "success": False,
                        "data": {"requires_answer": True,
                                 "candidate": candidate,
                                 **{k: v for k, v in asked.items()
                                    if k == "question_id"}},
                        "error": asked.get("error") or
                                 ("This needs your permission first: installing "
                                  "a market skill runs code fetched from the "
                                  "internet. Ask the user and wait for their "
                                  "answer before installing anything."),
                        "forged": False,
                    }
                installed_name = install_match(candidate)
                if installed_name:
                    result = await skill_registry.execute(installed_name, params)
                    return {
                        "success": result.success,
                        "data": result.data,
                        "error": result.error,
                        "forged": False,
                        "market": True,
                        "installed_skill": installed_name,
                    }
    except Exception as e:
        log.debug("market search failed: %s", e)

    # The forge reads this and searches the web with it, so it has to describe
    # the *capability* rather than parrot the function name back. Called
    # `scrape_website`, the old text produced the web query "python library
    # implement a function called scrape_website pip install", which discovers
    # nothing; the task the user actually asked for discovers a real library.
    try:
        from backend.config import config as _cfg
        request = str(_cfg.get("_forge", "request", default="") or "").strip()
    except Exception:
        request = ""
    task_text = (f"{request}\n\n(The assistant needs a tool named '{name}' "
                 f"that takes these parameters: {json.dumps(params)}.)"
                 if request else
                 f"A tool named '{name}' that accepts these parameters: "
                 f"{json.dumps(params)}")

    # Forging WRITES AND RUNS new code, so it asks first for the same reason the
    # market install does — arguably more, since the code did not exist anywhere
    # until this moment and nothing has reviewed it.
    #
    # Skipped entirely while `ask_before_build` is on, because in that mode the
    # user has ALREADY been asked — `_offer_to_build` above raises "build one, or
    # search?" and ends the turn. Asking a second, differently-worded permission
    # question about the same unmet capability is how a user learns to click
    # through prompts, and it also made the `needs_tool` offer unreachable: the
    # forge question always fired first, so the end of the chain was never
    # reached and the Settings pointer was never shown.
    ask_to_forge = not _build_offer_enabled()
    if ask_to_forge and not _consented(None):
        asked = await _ask_to_acquire(
            "write and test a new skill",
            f"a tool called '{name}'",
            "It searches the web for a library, generates the code, and runs it "
            "to check that it works.",
            ["Write it", "Skip this"])
        return {
            "success": False,
            "data": {"requires_answer": True,
                     **{k: v for k, v in asked.items() if k == "question_id"}},
            "error": asked.get("error") or
                     ("This needs your permission first: forging writes new code "
                      "and runs it. Ask the user and wait for their answer before "
                      "generating anything."),
            "forged": False,
        }

    try:
        from backend.skills.forge import skill_forge

        # Try to discover and create this skill
        forge_result = await skill_forge.forge(
            task_description=task_text,
            provider=forge_target_provider(provider),
            auto_validate=True,
        )

        if forge_result.success:
            # Re-execute with the newly forged skill
            result = await skill_registry.execute(name, params)
            return {
                "success": result.success,
                "data": result.data,
                "error": result.error,
                "forged": True,
                "forge_detail": forge_result.detail,
            }

        # Nothing was found and nothing could be written. This is not an error
        # to narrate — it is the moment the user is told they can build one, and
        # the offer has to be shaped like a question or the loop drops it into
        # the generic failure path and the model reports a dead end.
        return await _needs_tool(name, params,
                                 f"the forge could not write it: "
                                 f"{forge_result.detail}")
    except Exception as e:  # noqa: BLE001
        return await _needs_tool(name, params, f"nothing could be built: {e}")


async def _needs_tool(name: str, params: dict, why: str = "") -> dict:
    """The end of the chain: nothing exists, nothing could be acquired.

    Not a dead end. Every earlier step is a way of FINDING a tool; this is the
    one that says how to MAKE one, which is the whole premise of the feature —
    the user can build a tool deliberately instead of waiting for the model to
    forge one mid-turn.

    It is raised as a QUESTION, through the same `pending.ask` the acquisition
    prompts use, and that is not decoration. The loop stops a turn on
    `requires_answer` and reports `question`/`options`/`question_id` at the TOP
    LEVEL of the result; a result that only carried a `data.needs_tool` block
    fell through to the generic failure branch instead, so the model was handed
    "I have no tool for this" and narrated it as an error. The offer never
    reached the user, which is exactly what this function exists to prevent.

    `needs_tool` rides alongside the question fields, so the card and the deep
    link into Settings have the capability and a suggested name to prefill.
    """
    capability = _buildable_capability(name, params)
    detail = f" ({why})" if why else ""
    asked = await _ask_to_acquire(
        "build a small tool for this",
        capability,
        f"I have no tool for it and nothing suitable could be found{detail}. "
        f"It would be written by the model, shown to you to review before "
        f"anything is saved, and then used from chat, code and your swarm "
        f"agents like any other skill.",
        ["Build it", "Never mind"])
    needs = {
        "capability": capability,
        "suggested_slug": _slug_hint(name or capability),
        "called_name": name,
    }
    if not asked.get("success"):
        # Unattended, or a question already open. Still an offer-shaped result,
        # so the caller can tell the user where to build one even though no card
        # was raised.
        return {
            "success": False,
            "data": {"requires_answer": True, "needs_tool": needs},
            "error": (
                f"I have no tool for this and nothing suitable could be "
                f"found{detail}. One can be built: Settings -> CLI Tools, ask "
                f"for \u201c{capability}\u201d. Tell the user that rather than "
                f"guessing at the answer or inventing a tool call."
            ),
            "forged": False,
        }
    return {
        "success": False,
        "data": {
            "requires_answer": True,
            "needs_tool": needs,
            **{k: v for k, v in asked.items() if k == "question_id"},
        },
        # Top level, because that is where the loop looks for it.
        "question": asked.get("question"),
        "options": asked.get("options") or [],
        "question_id": asked.get("question_id"),
        "ttl": asked.get("ttl"),
        "requires_answer": True,
        "error": (
            f"I have no tool for this and nothing suitable could be "
            f"found{detail}. I can build one — Settings -> CLI Tools, ask for "
            f"\u201c{capability}\u201d. Tell the user that rather than guessing "
            f"at the answer or inventing a tool call."
        ),
        "forged": False,
    }


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


# How many times a turn may be told to show its work before it is allowed to
# finish anyway.
#
# One, not three. The nudge is for the honest case - the model changed a file and
# simply did not say what it checked - and a model that is going to verify does
# it on the first ask. A second and third ask mostly produce a restatement of the
# first, and each one costs a model call in a turn the user is waiting on.
MAX_VERIFY_NUDGES = 1

# A turn that says it is about to act and then emits nothing.
#
# The other half of the 2026-10-10 report, and the half with no syntax to catch.
# The reply was "On it - searching for Flux model files under the aethelgard
# tree now" with no call, no fence and no tag, so every reader in this file
# returned `[]` and the turn was returned to the user as the answer. It reads
# as work in progress and is in fact a dead end: the user's next message was
# "how is it ?", and the model could only admit "nothing has been searched."
#
# Detection is STRUCTURAL, not a phrase list. A list of words would fire on
# ordinary answers - "I'll explain how X works" is not a promise to act - so the
# nudge only happens when all of these hold: tools were offered this turn, the
# model called none of them, nothing has run yet, and the text claims the action
# is under way. That is the shape of a dropped call and nothing else.
#
# One, because a model that is going to act does it on the first ask, and a
# second costs a model call in a turn the user is waiting on.
MAX_PROMISE_NUDGES = 1

# A claim that the work is happening NOW, rather than a description of what
# could be done. Tensed deliberately: "I'll run it" is a plan, "I ran it" is a
# false completion (a different defect), and "running it now" is the one this
# catches.
#
# The frame, not the verb. Three live attempts on the install produced three
# different wordings for the same intent -- "Deleting that temp folder now",
# "Firing the delete now", "Going ahead with the delete now" -- and each one
# was missed by a verb whitelist that did not happen to contain it. Enumerating
# verbs is a race the model wins by inventing the next one, so the test is
# grammatical instead: an ACTION GERUND leading a clause, with an immediacy
# marker ("now", "right now", "as we speak") inside that clause. Any verb at
# all qualifies, which is the point -- the marker is what makes it a claim of
# present action. "Deleting files is dangerous" has a gerund and no marker and
# is an explanation; "the running total is 42" has no leading gerund.
_PROMISE_GERUND = re.compile(
    r"(?:^|[.!?]\s+|,\s+|\band\s+|\bi'?m\s+|\bi am\s+)"
    r"\w+ing\b[^.!?\n]{0,40}?\b(?:now|right now|as we speak)\b",
    re.IGNORECASE)

# A stated intention to act, in the first person: "Let me fire the command",
# "I'll carry it out". The verb is anything, for the same reason as above; what
# keeps "I'll explain how the parser works" out is the caller's other tests (a
# tool was offered, none was called, nothing has run) together with the
# short-reply cap -- an explanation runs long, a promise is one line.
_PROMISE_INTENT = re.compile(
    r"\b(?:let me|i'?ll|i will|i'?m going to)\s+(?:\w+\s+){0,3}?\w+",
    re.IGNORECASE)

# Announcing that the action is going ahead, with no subject at all. These are
# idioms rather than a verb class, so they are listed: they are finite and the
# model's own.
_PROMISE_IDIOM = re.compile(
    r"\b(?:on it|going ahead|firing|executing|carrying it out|"
    r"following through|taking care of it)\b", re.IGNORECASE)

# Verbs that describe TALKING about something rather than doing it. This is a
# deny-list, deliberately the opposite shape from the verb whitelist this code
# used to carry: the frames above do not enumerate what a promise can say, and
# this only removes the narrow case an explanation shares with a promise's
# opening ("I'll explain how X works"). Closed and tiny on purpose.
_EXPLAINS_PAT = re.compile(
    r"\b(?:explain|describe|summari[sz]e|clarify|outline|walk you through|"
    r"note that|mention|point out|answer|tell you about)\b", re.IGNORECASE)

# Offering to carry the action out, as a sentence of its own rather than a verb
# phrase: "I'll carry it out and confirm it's gone".
_CARRY_OUT_PAT = re.compile(
    r"\b(?:i'?ll|i will|let me)\s+(?:go ahead and\s+)?"
    r"(?:carry|follow through|take care of)\b", re.IGNORECASE)


# Parameter names that mean "this call touched a file on disk".
#
# Keyed on the ARGUMENT rather than the tool name on purpose. A tool-name list
# covers the built-ins and silently misses every skill the user installed or
# forged - exactly the tools a particular person added to do their own work. A
# `path` argument is what actually makes a call a write, whoever registered it.
_WRITE_PATH_ARGS = ("path", "file", "filename", "filepath", "dest",
                    "destination", "target")

# Run-of-the-mill commands that are evidence of nothing. If the only thing the
# turn ran was one of these, it has not checked its work.
_NON_VERIFYING = (
    "cat ", "type ", "echo ", "ls", "dir", "pwd", "cd ", "head ", "tail ",
    "which ", "where ", "whoami", "date", "time",
)


def _params_touched_a_file(params) -> bool:
    """Did this call name a file to write to?"""
    if not isinstance(params, dict):
        return False
    for key in _WRITE_PATH_ARGS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _turn_changed_files(tool_results: list[dict]) -> bool:
    """Whether the turn wrote to disk. Only these turns need verification."""
    for tr in tool_results or []:
        if not tr.get("success"):
            continue
        result = tr.get("result")
        if isinstance(result, dict) and _params_touched_a_file(result):
            return True
    return False


def _turn_verified(tool_results: list[dict]) -> bool:
    """Whether the turn ran something that could show the change worked.

    Deliberately generous. This decides whether to spend a model call asking for
    proof, and a false "it already checked" only means one fewer nudge - while a
    false "it did not check" nags a turn that had in fact run its tests. Where
    the two errors differ, this errs toward silence.
    """
    for tr in tool_results or []:
        if not tr.get("success"):
            continue
        name = str(tr.get("tool") or "").lower()
        if any(k in name for k in ("run", "exec", "terminal", "shell", "test",
                                   "check", "verify", "build", "compile",
                                   "cli", "lint")):
            return True
        result = tr.get("result")
        if isinstance(result, dict):
            cmd = str(result.get("command") or result.get("cmd") or "")
            low = cmd.strip().lower()
            if low and not any(low.startswith(s) for s in _NON_VERIFYING):
                # A real command was run, not just a listing.
                if result.get("stdout") or result.get("stderr") or result.get("exit_code") is not None:
                    return True
    return False


def _announced_work(text: str) -> bool:
    """Whether this reply claims the action is happening now.

    Only ever consulted in the one shape that makes it meaningful - see
    `MAX_PROMISE_NUDGES`. On its own the phrase means nothing; a reply that
    says "let me explain" and then explains is doing exactly what it said.
    """
    if not text:
        return False
    stripped = str(text).strip()
    if not stripped:
        return False
    # The gerund and idiom frames are precise - a leading action gerund with an
    # immediacy marker does not occur in an explanation, and the idioms are the
    # model's own words - so they are tested on the whole reply. The live
    # destructive reply was one promise then explanation, and another live turn
    # put the promise in the SECOND sentence ("Understood - your call. Firing
    # the delete now"), which a first-sentence-only test would miss.
    if _PROMISE_GERUND.search(stripped) or _PROMISE_IDIOM.search(stripped):
        return True
    if _CARRY_OUT_PAT.search(stripped):
        return True
    # The first-person intent frame ("let me...", "I'll...") is broad, so it
    # needs both guards: a length cap (an explanation runs long, a promise is
    # one line) and a deny-list of verbs that describe talking rather than
    # acting. The deny-list is deliberately tiny and closed - the point of the
    # other frames is to avoid enumerating what a promise CAN say; this only
    # removes what it demonstrably does not ("I'll explain how the parser
    # works").
    if len(stripped) > 700:
        return False
    if _EXPLAINS_PAT.search(stripped):
        return False
    return bool(_PROMISE_INTENT.search(stripped))


def _tools_were_offered(only: set[str] | None) -> bool:
    """Whether this turn gave the model a tool catalogue to call.

    The promise nudge needs this to mean what its comment says - "tools have
    been offered" - and it used to test `only` directly, which is the OPPOSITE:
    `only` is None for every enabled skill, so the nudge was skipped on exactly
    the surface that needs it most. Measured live on 2026-10-10: the dashboard
    sent `tools=None`, the model answered a destructive request with "Running it
    now - it may come back asking you to approve", called nothing, and the nudge
    never fired because `None` is falsy. The user got an announcement instead of
    a permission card, which is the whole bug this guard exists for.

    None means the full catalogue, and a non-empty set means that subset; both
    are "tools were offered". An empty set is the only case where the model was
    given nothing to call, and there the nudge would be asking for the
    impossible - so it is the one case that returns False. A failure resolving
    the catalogue means ASK rather than skip, the same direction every other
    guard here takes.
    """
    if only is not None and not only:
        return False
    try:
        return bool(skill_registry.to_openai_tools(only))
    except Exception as e:  # noqa: BLE001
        log.debug("could not resolve the tool catalogue (%s); nudging anyway", e)
        return True


# What each tool is doing, said in the user's terms rather than the tool's.
#
# Derived from the name, deliberately: the tool catalogue is user-extensible
# (market skills, forged skills, MCP servers all register names this file has
# never seen), so a lookup table would be silently blank for exactly the tools a
# particular user added. A verb is claimed only for the names whose verb is
# unambiguous, and everything else falls back to the name itself - which is at
# least true, and is what the user is already reading in the transcript.
_ACTIVITY_VERBS = (
    ("search", "Searching"), ("find", "Searching"),
    ("read", "Reading"), ("open", "Opening"), ("list", "Listing"),
    ("write", "Writing"), ("create", "Creating"), ("mkdir", "Creating"),
    ("edit", "Editing"), ("replace", "Editing"), ("patch", "Editing"),
    ("delete", "Deleting"), ("remove", "Removing"),
    ("run", "Running"), ("exec", "Running"), ("terminal", "Running"),
    ("shell", "Running"), ("cli", "Running"), ("git", "Running"),
    ("fetch", "Fetching"), ("http", "Fetching"), ("download", "Downloading"),
    ("web", "Looking that up"), ("browse", "Browsing"),
    ("memory", "Checking memory"), ("recall", "Checking memory"),
    ("wiki", "Checking the wiki"), ("journal", "Checking the journal"),
    ("screenshot", "Taking a screenshot"), ("vision", "Looking at the image"),
    ("transcribe", "Listening"), ("speak", "Speaking"),
    ("swarm", "Delegating"), ("delegate", "Delegating"),
    ("subagent", "Delegating"), ("spawn", "Delegating"),
    ("image", "Making an image"), ("generate", "Generating"),
    ("code", "Working on the code"), ("plan", "Planning"),
    ("install", "Installing"), ("build", "Building"),
)


def _activity_for(tool_name: str) -> str:
    """A short, true description of what a tool call is doing.

    Returns something human - "Searching the web" - for the names whose verb is
    clear, and the bare tool name otherwise. Never raises, and never empties:
    the fallback is the name, because a blank status is worse than a raw one.
    """
    name = str(tool_name or "").strip()
    if not name:
        return "Working"
    low = name.lower()
    for needle, verb in _ACTIVITY_VERBS:
        if needle in low:
            # Keep the tail when it is short and readable, so "Searching the web"
            # beats "Searching" for web_search, while a synthetic name does not
            # produce "Searching x9_f_2".
            words = [w for w in low.replace("_", " ").split()
                     if w and not w.isdigit() and len(w) > 2]
            rest = " ".join(w for w in words if w != needle)
            if rest and len(rest) <= 24:
                return f"{verb} {rest}"
            return verb
    return name.replace("_", " ").capitalize()


def _learn_procedure(messages: list[dict], tool_results: list[dict],
                     system_prompt: str = "") -> None:
    """Keep the route a turn actually took, when it worked.

    Called on the way out of a tool-using turn. It is a side effect of a reply
    that has already been produced, so it must never raise and must never
    delay or alter the response.

    `system_prompt` is passed so the recipe can carry a one-line reason. It is
    the same context the turn was actually given — recalled facts, wiki pages,
    and the procedure that was offered — so storing a fragment of it records
    WHY this route was chosen, not just which tools were used. A list of tool
    names tells the next run what happened; it does not tell it what the model
    knew at the time.
    """
    try:
        if not tool_results:
            return
        if not any(tr.get("success") for tr in tool_results):
            return
        from backend.sop.learn import record_run
        names = [str(tr.get("tool") or "") for tr in tool_results]
        record_run(None, names, _last_user_text(messages), success=True,
                   context=system_prompt)
    except Exception as e:
        log.debug("Procedure learning skipped: %s", e)

def _queue_review(messages: list[dict], tool_results: list[dict],
                  reply: str = "") -> None:
    """Offer a finished turn to the post-turn review.

    Separate from `_learn_procedure` on purpose, and called alongside it. That
    one records the ROUTE mechanically and always will - it is cheap, it needs
    no model, and a turn that used tools got somewhere worth remembering. This
    one asks a model whether the turn taught something the route does not
    capture: a pitfall, a command that had to be exact, a fact about the user.
    The two answer different questions and neither replaces the other.

    Queues and returns. The model call happens later, on the engine tick, when
    the machine is quiet - never here, because "here" is the moment the next
    prompt wants the same model.
    """
    try:
        from backend.review import queue_review
        names = [str(tr.get("tool") or "") for tr in (tool_results or [])]
        errors = [str(tr.get("error") or "") for tr in (tool_results or [])
                  if not tr.get("success")]
        trace = "tools: " + (", ".join(names) or "(none)")
        if errors:
            trace += "\nfailures: " + "; ".join(e for e in errors if e)[:500]
        queue_review(_last_user_text(messages), trace,
                     message=_last_user_text(messages), outcome=reply)
    except Exception as e:
        log.debug("Review queueing skipped: %s", e)


async def chat_with_tools(
    provider,
    messages: list[dict],
    system_prompt: str = "",
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    model: str | None = None,
    tools: list[str] | None = None,
    reply_directive: str = "",
    learn: bool = True,
    on_delta=None,
    on_activity=None,
) -> dict:
    """
    Run a chat completion with automatic tool execution.

    Flow:
      1. Send messages + tools to provider
      2. If provider returns a tool_call → execute skill → append result → repeat
      3. If provider returns text → done, return response

    ``model`` is chosen by the request router (see backend.providers.router).
    When it is None the provider uses its own configured default, which is the
    behaviour Addled had before routing existed.

    ``tools`` narrows the catalogue to those skill names. None means every
    enabled skill, which is what the chat page, the character, voice and the
    bots all use. A list is for a caller that wants a defined subset — it is
    the same catalogue either way, not a second one, so a skill is offered on
    every path unless the caller deliberately restricts it.

    ``reply_directive`` goes last in the turn being answered, after the tool
    catalogue. That placement is the point: the catalogue is appended to this
    same message and is thousands of tokens of English, which is enough to make
    a small model answer in English whatever the system prompt said. See
    backend/language.py.

    ``on_delta``, when given, is called with each text chunk of the FINAL
    round as it arrives from the provider, so a surface can draw the answer
    while the model is still writing it. Only the final round streams: an
    earlier round is narration the model often discards, and a tool round cannot
    stream at all because a stream carries no tool calls. It is None by default,
    and None means no streaming path is taken.

    ``learn=False`` stops this turn from being recorded as a procedure.
    A caller that runs the model for its OWN reasons must use it: the Code
    page's planner makes two internal calls (search, then plan) that the user
    never sees, and every one of them used to be learned as if it were a task
    the user had asked for. The result was a store slowly filling with recipes
    titled after prompts like "do something", which then competed with the real
    built-ins when matching. `record=False` on the pipeline already stops the
    history, memory and journal writes; this is the same switch for learning,
    which lives here and so could not see that flag.
    """
    provider_id = getattr(provider, "provider_id", "unknown")
    uses_native = (
        provider_id in NATIVE_TOOL_PROVIDERS
        or type(provider).__name__ == "OpenAIProvider"
        or getattr(provider, "has_native_tools", False)
    )
    # None means "no filter"; an empty list means "no tools", which is a
    # distinction a caller passing a restricted set depends on.
    only = set(tools) if tools is not None else None

    # Build full message list
    full_messages = []
    if system_prompt:
        full_messages.append({"role": "system", "content": system_prompt})
    full_messages.extend(messages)

    # Some endpoints accept the `system` role and quietly discard it (9router
    # does — see backend/providers/system_role.py). When this provider is
    # KNOWN to drop it, the system text rides in the first user turn instead,
    # because the alternative is every instruction and every memory block
    # being silently lost. Providers that keep it are untouched.
    from backend.providers import system_role
    full_messages = system_role.prepare(provider, full_messages)

    async def _final_answer(tool_results):
        """One more LLM call forced to plain text, using gathered results.

        This is a KNOWN-final call - the prompt below forbids further tool use -
        so it is the one place a tool-using turn can be streamed. Rounds from the
        loop cannot be, since a stream carries no tool calls.
        """
        full_messages.append({
            "role": "user",
            "content": ("Answer the user's question now, based on the "
                        "information gathered above. Do not call any more "
                        "tools — respond with plain text."),
        })
        try:
            if uses_native:
                final = await _call_native_tools(provider, full_messages, model,
                                                 only, reply_directive,
                                                 on_delta=on_delta, final=True)
            else:
                final = await _call_prompt_tools(provider, full_messages, model,
                                                 only, reply_directive,
                                                 on_delta=on_delta, final=True)
            final_text = (final.get("response") or "").strip()
            if final_text and not final.get("tool_calls"):
                return {
                    "response": final_text,
                    "tokens": final.get("tokens", 0),
                    "tool_rounds": rounds,
                    "tool_results": tool_results if tool_results else [],
                }
        except Exception as e:
            log.warning("Final summarization call failed: %s", e)
        return None

    rounds = 0
    verify_nudges = 0
    promise_nudges = 0
    # Every tool result from every round. The per-round list below is what the
    # failure and round-cap paths reason about; this accumulates so a normal
    # text reply still reports what was actually called.
    all_tool_results: list[dict] = []
    while rounds < max_tool_rounds:
        rounds += 1

        # Try the round as a stream first, then decide.
        #
        # This is the case that actually matters and the one the first version
        # missed: a plain answer with no tool call is the COMMONEST turn there
        # is, and it never reached `_final_answer`, so it never streamed at all.
        # Streaming only the forced plain-text round meant the feature worked for
        # tool-using turns and did nothing for an ordinary question.
        #
        # It is safe because the stream is buffered, not shown as it arrives: on
        # this path a round CAN carry a tool call (that is how prompt-tools
        # providers work), so the text is held until it is known to contain no
        # call. If it does, nothing is emitted, the round is re-run on the batch
        # path, and the tool executes exactly as before. The re-run is the price
        # of streaming this round; it only happens on rounds that call a tool.
        if on_delta is not None:
            streamed = await _stream_round(provider, full_messages, model, only,
                                           reply_directive)
            if streamed is not None:
                result = streamed
            else:
                # A tool call, or the stream failed: take the normal path.
                if uses_native:
                    result = await _call_native_tools(
                        provider, full_messages, model, only, reply_directive)
                else:
                    result = await _call_prompt_tools(
                        provider, full_messages, model, only, reply_directive)
        elif uses_native:
            result = await _call_native_tools(provider, full_messages, model,
                                              only, reply_directive)
        else:
            result = await _call_prompt_tools(provider, full_messages, model,
                                              only, reply_directive)

        # The streamed round carried no tool call, so its text IS normally the
        # answer - emit it and finish.
        #
        # EXCEPT when it claims the action is under way. That is the one shape
        # where a no-call round is not an answer: measured live on 2026-10-10,
        # the dashboard's streaming round returned "Deleting that temp folder
        # now ... tap Allow on the card above the composer" with no call and no
        # card, and this early return emitted it as the finished turn - skipping
        # the promise nudge below entirely, because a streamed round never
        # reached it. The nudge was unreachable on the ONE surface it was built
        # for. A promise-like round is therefore NOT returned here; it falls
        # through to the nudge, which retries and, failing that, answers.
        if result.get("streamed") and not _announced_work(
                result.get("response", "")):
            if on_delta is not None:
                try:
                    on_delta(result.get("response", ""))
                except Exception:
                    log.debug("stream listener failed", exc_info=True)
            if learn:
                _learn_procedure(messages, all_tool_results, system_prompt)
            return {
                "response": result.get("response", ""),
                "tokens": result.get("tokens", 0),
                "tool_rounds": rounds,
                "tool_results": all_tool_results,
            }

        # No tool call — normal text response, unless the reply was a tool call
        # the parser could not read. That must never be shown as an answer: the
        # model's own "I have created the file" is in it, and nothing ran.
        if not result.get("tool_calls"):
            unreadable = [b for b in (result.get("malformed") or []) if b]
            if unreadable:
                log.warning("A tool call could not be read: %s",
                            unreadable[0][:300])
                if rounds < max_tool_rounds:
                    # One corrective round: telling the model what shape to use
                    # is far cheaper than a user hunting for a file that was
                    # never written.
                    hint = ("Your last reply contained a tool call that could "
                            "not be read, so nothing ran and nothing was "
                            "created or changed. Reply again with only this "
                            "shape, with no other text:\n")
                    hint += skill_registry.PROMPT_CALL_FORMAT
                    hint += "\n\nWhat you wrote was:\n" + unreadable[0][:400]
                    full_messages.append({"role": "user", "content": hint})
                    continue
                if learn:
                    _learn_procedure(messages, all_tool_results, system_prompt)
                return {
                    "response": ("I tried to call a tool for that, but the "
                                 "call could not be read, so nothing ran — "
                                 "nothing was created or changed. Ask again, "
                                 "or in smaller steps."),
                    "tokens": result.get("tokens", 0),
                    "tool_rounds": rounds,
                    "tool_results": all_tool_results,
                    "unreadable": unreadable,
                }
            # A turn that says it is acting and then emits nothing. The gap the
            # `malformed` path cannot cover, because there is no syntax to catch:
            # "On it - searching now" with no call, no fence and no tag.
            #
            # Gated on the whole shape rather than the phrase. It needs tools to
            # have been offered, NO call this round, NOTHING run yet this turn,
            # and the text to claim the action is under way. A first-round reply
            # with no call is otherwise a perfectly good answer - it is the claim
            # of action on top of it that makes it a dropped call.
            if (promise_nudges < MAX_PROMISE_NUDGES
                    and not all_tool_results
                    and _tools_were_offered(only)
                    and _announced_work(result.get("response", ""))):
                promise_nudges += 1
                full_messages.append({
                    "role": "assistant",
                    "content": result.get("response", ""),
                })
                full_messages.append({
                    "role": "user",
                    "content": (
                        "You said you were running or searching for that, but "
                        "no tool was called, so nothing has actually started and "
                        "there is no result to report. Either call the tool now "
                        "in this shape, with no other text:\n"
                        + skill_registry.PROMPT_CALL_FORMAT +
                        "\n\nOr, if no tool is needed, answer the question "
                        "directly instead of describing what you would do."),
                })
                log.info("Promise nudge: announced action, nothing called")
                continue
            # Show your work before you claim it works.
            #
            # A turn that changed files and ran nothing is the turn that says "I
            # fixed it" with no evidence, and the user finds out later. Ask for
            # the evidence once, and only for a turn that actually wrote
            # something: a conversational turn has nothing to verify, and
            # nagging one would be the cost of this feature with none of the
            # benefit. Bounded, so it cannot loop.
            _results_so_far = (all_tool_results
                               or result.get("tool_results", []))
            if (verify_nudges < MAX_VERIFY_NUDGES
                    and _turn_changed_files(_results_so_far)
                    and not _turn_verified(_results_so_far)):
                verify_nudges += 1
                full_messages.append({
                    "role": "assistant",
                    "content": result.get("response", ""),
                })
                full_messages.append({
                    "role": "user",
                    "content": (
                        "You changed files in this turn but ran nothing that "
                        "shows the change works. Tell the user what you changed "
                        "and run something that demonstrates it - a test, the "
                        "program, or the command that reads the result back. If "
                        "nothing can be run, say plainly that it is unverified "
                        "and why."),
                })
                log.info("Verification nudge: files changed, nothing ran")
                continue

            if learn:
                _learn_procedure(messages, all_tool_results
                                 or result.get("tool_results", []), system_prompt)
                _queue_review(messages,
                              all_tool_results
                              or result.get("tool_results", []),
                              result.get("response", ""))
            # A streamed round held back for the promise nudge and then accepted
            # as the answer (budget spent, or the phrase was an ordinary answer).
            # Its deltas were buffered and never emitted, so emit them now or the
            # streaming surface shows nothing while the transcript gains a reply.
            # Guarded on `streamed` so a batch round is not emitted twice.
            if result.get("streamed") and on_delta is not None:
                try:
                    on_delta(result.get("response", ""))
                except Exception:
                    log.debug("stream listener failed", exc_info=True)

            return {
                "response": result.get("response", ""),
                "tokens": result.get("tokens", 0),
                "tool_rounds": rounds,
                "tool_results": (all_tool_results
                                 or result.get("tool_results", [])),
            }

        # Execute tool calls (with auto-forge for missing skills)
        tool_results = []
        executed: list[tuple[dict, dict]] = []
        for tc in result["tool_calls"]:
            # Say what is running, before it runs. A tool round is the long
            # silence in a turn - a web search or a model load is seconds - and
            # until now nothing was emitted during it at all, so the surface sat
            # on a spinner with no idea whether Addled was working or stuck.
            #
            # This is the tool NAME the loop already has, not generated prose:
            # no extra model call, and nothing that can be untrue. Best-effort,
            # because a failed notice must never cost the turn it describes.
            if on_activity is not None:
                try:
                    on_activity(_activity_for(tc["name"]))
                except Exception:
                    log.debug("activity notice failed", exc_info=True)
            exec_result = await execute_skill(
                tc["name"], tc.get("params", {}), provider)
            log.info("Tool call: %s(%s) -> success=%s error=%s",
                     tc["name"], json.dumps(tc.get("params", {}))[:200],
                     exec_result["success"], str(exec_result.get("error"))[:200])
            tool_results.append({
                "tool": tc["name"],
                "success": exec_result["success"],
                "result": exec_result.get("data", {}),
                "error": exec_result.get("error"),
                "forged": exec_result.get("forged", False),
                "requires_approval": bool(
                    (exec_result.get("data") or {}).get("requires_approval")),
            })
            executed.append((tc, exec_result))

        # Echo back only the calls whose skill actually exists. A name the model
        # invented (and the forge could not create) must not appear in the
        # assistant message: the API rejects the entire follow-up request, which
        # would lose the answer instead of reporting an unknown tool.
        valid = [(tc, res) for tc, res in executed
                 if skill_registry.get(tc["name"])]
        # Pair every tool result with an assistant tool_call, and give each one
        # an id. The OpenAI tool protocol is a PAIR: a `role:"tool"` message is
        # only valid if it answers an assistant `tool_calls` entry with the
        # same id. Two shapes here broke that, and the failure was the same
        # 400 from the live gateway on 2026-10-10:
        #
        #   9router -> HTTP 400 {"code":11133, "msg":"Invalid request
        #   parameters", "extError":{"code":"model_param_invalid"}}
        #
        # 1. A text-written call (the fallback path, and providers without
        #    native tools) is parsed from prose and has NO id at all.
        # 2. A native call from a gateway that omits the id.
        #
        # In both cases the old code filtered `raw_tool_calls` by id, so an
        # id-less call echoed NO assistant message but still emitted a
        # `role:"tool"` message - a tool result with no call, which is exactly
        # `model_param_invalid`. Reproduced against the running 9router: a
        # `role:"tool"` with no `tool_call_id` returns 11133; the same request
        # with an id returns 200.
        #
        # So the id is now MADE here when the model did not supply one, and the
        # assistant call is built from the executed calls rather than from
        # whatever the provider happened to echo. Every valid result therefore
        # has a matching call, by construction, on every path.
        raw_by_id = {raw.get("id"): raw
                     for raw in (result.get("raw_tool_calls") or [])
                     if raw.get("id")}
        paired: list[tuple[dict, dict, str]] = []
        for index, (tc, exec_result) in enumerate(valid):
            call_id = tc.get("id") or "addled_call_%d" % index
            tc["id"] = call_id
            paired.append((tc, exec_result, call_id))
        if paired:
            # Keep the assistant tool_call message in history (required by
            # OpenAI-compatible APIs for the follow-up request). A call the
            # gateway echoed is reused verbatim; one it did not (or that had no
            # id) is rendered in the OpenAI shape so the pair is complete.
            assistant_tool_calls = []
            for tc, _res, call_id in paired:
                raw = raw_by_id.get(call_id)
                if raw is not None:
                    assistant_tool_calls.append(raw)
                else:
                    assistant_tool_calls.append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("params", {})),
                        },
                    })
            assistant_message: dict = {
                "role": "assistant",
                "content": result.get("response") or None,
                "tool_calls": assistant_tool_calls,
            }
            # Thinking-mode models (DeepSeek v4) reject the follow-up request
            # unless the reasoning_content they returned is passed back. Only
            # set it when the provider actually sent it, so providers that
            # never do are not handed an unknown field.
            reasoning = result.get("reasoning_content")
            if reasoning:
                assistant_message["reasoning_content"] = reasoning
            full_messages.append(assistant_message)
        for tc, exec_result, call_id in paired:
            # Add to message history so provider sees the result. The id is
            # always present, so the pair above is always matched.
            full_messages.append(
                _tool_result_message(tc["name"], exec_result, call_id))
        all_tool_results.extend(tool_results)

        # An MCP approval gate is built for one retry: the model asks, the user
        # agrees, and the same tool is called again with confirm=true. This
        # nudges exactly that, and only for MCP tools — `confirm` is an MCP
        # argument, so sending a shell command round again would just repeat a
        # call that cannot succeed. A gated command needs no nudge at all: the
        # turn ended when it asked, the request is queued, and approving it runs
        # the action and pushes the outcome into the conversation. Telling the
        # model to wait would only make it narrate a wait nobody is holding.
        if (rounds < max_tool_rounds
                and any(str(tr.get("tool") or "").startswith("mcp__")
                        and (tr.get("result") or {}).get("requires_approval")
                        for tr in tool_results)):
            full_messages.append({
                "role": "user",
                "content": ("If the user has already agreed to run the refused "
                            "MCP tool, call that same tool again with the same "
                            "arguments plus confirm=true. Do not call a separate "
                            "trust-check or verification tool unless the user "
                            "asked for one."),
            })
            continue

        # A question is the end of the turn, not a step in it.
        #
        # `ask_user` queues the question and returns success, so without this
        # the loop would carry on and hand the result back to the model, which
        # would then answer its own question or narrate a wait that nobody is
        # holding. The turn stops here: the card is on screen, and the user's
        # answer arrives as the next turn. Returning early is also what keeps
        # the transcript honest — nothing after a question has happened yet.
        asked = [tr for tr in tool_results
                 if (tr.get("result") or {}).get("requires_answer")]
        if asked:
            first = (asked[0].get("result") or {})
            question = str(first.get("question") or "").strip()
            options = first.get("options") or []
            ttl = str(first.get("ttl") or "a few minutes")
            if options:
                shown = "; ".join(str(o) for o in options[:6])
                body = f"\n\n{shown}"
            else:
                body = ""
            return {
                "response": (f"{question}{body}\n\n"
                             f"I'll carry on when you answer — the question "
                             f"stays open for {ttl}."),
                "tokens": 0,
                "tool_rounds": rounds,
                "tool_results": tool_results,
                # Read by the pipeline so the turn is recorded as waiting on an
                # answer rather than as a completed exchange.
                "awaiting_answer": True,
                "question_id": first.get("question_id"),
            }

        # A permission request is not a failure, and must not be reported as one.
        #
        # `execute_for_chat` returns success=False for a gated action so the
        # caller knows nothing ran, and it marks the result `requires_approval`.
        # Feeding that into the all-failed branch below produced "I couldn't run
        # the tool for that: run_command — <error>" and invited the model to
        # invent a cause — the user saw a failed attempt where a permission
        # prompt had been raised. Asking is a distinct outcome, so it is
        # separated from failure before that branch is reached.
        pending = [tr for tr in tool_results
                   if (tr.get("result") or {}).get("requires_approval")]
        if pending and not any(tr["success"] for tr in tool_results):
            said = ", ".join(str(tr.get("tool") or "the action")
                             for tr in pending[:3])
            return {
                "response": (f"This needs your approval before it can run: "
                             f"{said}. Approve it in the dashboard and I will "
                             f"pick up where I left off."),
                "tokens": 0,
                "tool_rounds": rounds,
                "tool_results": tool_results,
                # Read by the pipeline so the turn is recorded as waiting on a
                # decision rather than as a completed exchange — and so a
                # resume knows this turn was interrupted rather than finished.
                "awaiting_approval": True,
            }

        # If all tools failed, try a forced plain-text answer before giving up
        if all(not tr["success"] for tr in tool_results):
            final = await _final_answer(tool_results)
            if final is not None:
                return final
            # Name them. "I tried to use some tools but they didn't work" gave
            # the user nothing to act on and hid the reason from a bug report.
            named = "; ".join(
                f"{tr['tool']} — {tr.get('error') or 'no error reported'}"
                for tr in tool_results[:3])
            return {
                "response": ("I couldn't run the tool for that: " + named),
                "tokens": 0,
                "tool_rounds": rounds,
                "tool_results": tool_results,
            }

    # Round cap reached: force one final answer from the gathered results.
    final = await _final_answer(tool_results if tool_results else [])
    if final is not None:
        if learn:
            _learn_procedure(messages, all_tool_results or tool_results,
                             system_prompt)
            # The turn that ends here is the one worth reviewing: the model
            # gathered results, then answered. A route was taken AND a reply was
            # produced, which is exactly the pair the review needs to judge what
            # was learned rather than merely what was done.
            _queue_review(messages, all_tool_results or tool_results,
                          final.get("response", ""))
        return final

    return {
        "response": "I've completed the requested actions.",
        "tokens": 0,
        "tool_rounds": rounds,
        "tool_results": tool_results if tool_results else [],
    }


def _with_directive(messages: list[dict], directive: str) -> list[dict]:
    """A copy of `messages` with the reply-language line at the very end.

    Left alone when there is nothing to attach it to, or when the last message is
    not the user's: inventing a turn to carry an instruction would put words into
    the conversation that nobody wrote.

    The copy matters as much as the placement — `full_messages` is reused across
    tool rounds, so appending in place would stack the directive once per round.
    """
    if not directive or not messages:
        return messages
    last = messages[-1]
    if last.get("role") != "user":
        return messages
    out = list(messages)
    out[-1] = {**last,
               "content": (last.get("content") or "") + "\n\n" + directive}
    return out

# A sentinel a streaming provider yields when a delta carries a NATIVE tool
# call. `chat_stream` is typed `AsyncIterator[str]`, so it has no channel to
# report one - and the OpenAI-compatible implementations read only
# `delta["content"]`, so a round of `content="I'll check that."` PLUS a
# `tool_calls` array lost the call entirely: the stream returned the
# announcement, `_stream_round` saw no call in the text, and the announcement
# was emitted as the final answer. Measured live against 9router/Voxagent on
# 2026-10-10 - the user's "check if whisper is installed" turn, where the model
# DID send two valid `run_command` calls and nothing ran, no permission card
# appeared, and nothing reached the console.
#
# A NUL-prefixed marker cannot collide with model text: a stream of real
# content never contains a NUL, and the value is discarded before either the
# buffer or `on_delta` sees it.
STREAM_TOOL_CALL = "\x00\x00addled:tool_call\x00\x00"


async def _streamed_answer(provider, messages: list[dict],
                           model: str | None = None,
                           max_tokens: int = 4096,
                           on_delta=None) -> dict:
    """Collect a streamed plain-text answer, emitting each chunk as it arrives.

    Only ever used for a call that is KNOWN to be final — ``_final_answer``'s
    forced plain-text round, or a round with no tools offered. It cannot be used
    for a tool round, and that is a fact about the provider API rather than a
    choice: ``Provider.chat_stream`` yields ``str`` only, so a streamed response
    carries no ``tool_calls`` field at all (see ``deepseek_provider.chat_stream``,
    which reads ``delta["content"]`` and nothing else). A round that might need
    a tool therefore has to stay on the batch call, because a stream could never
    tell us that a tool call was requested.

    The deltas are the point, not the buffer: ``on_delta`` fires synchronously as
    each chunk lands so the UI can draw text while the model is still writing.
    The joined text is returned in the same ``{"response", "tokens"}`` shape the
    batch path returns, so a caller cannot tell the two apart.

    ``on_delta`` defaults to None and every caller is expected to leave it that
    way unless a UI is attached — no callback, no queue, no event loop work, so
    a headless run costs exactly what it cost before.
    """
    chunks: list[str] = []
    # Whether any delta in this stream carried a NATIVE tool call. The
    # provider reports it with `STREAM_TOOL_CALL` rather than in the text,
    # because a stream of content and a tool call share one channel. A True
    # here means this round is a tool round and the caller must NOT treat the
    # text as the answer - see `_stream_round`.
    saw_tool_call = False
    try:
        async for piece in provider.chat_stream(
                messages, model=model, max_tokens=max_tokens, temperature=0.7):
            if not piece:
                continue
            if piece == STREAM_TOOL_CALL:
                saw_tool_call = True
                continue
            chunks.append(piece)
            if on_delta is not None:
                try:
                    on_delta(piece)
                except Exception:
                    # A broken listener must not cost the user their answer.
                    log.debug("stream listener failed", exc_info=True)
    except Exception as e:
        log.warning("Streamed answer failed: %s", e)
        if not chunks and not saw_tool_call:
            return {"response": "", "tokens": 0, "stream_failed": True}

    text = "".join(chunks)
    # A tool call with no prose is a COMPLETE round, not an empty one: the
    # OpenAI shape allows `content: null` alongside `tool_calls`, and a model
    # that has nothing to say before acting sends exactly that. Returning
    # `stream_failed` here would send the caller to the batch path for a round
    # that was never a failure - and, worse, would have thrown away the fact
    # that a call happened, which is the whole thing this flag exists to carry.
    if saw_tool_call:
        return {"response": text, "tokens": len(text) // 4,
                "stream_tool_call": True}
    if not text:
        return {"response": "", "tokens": 0, "stream_failed": True}

    # A stream that ends without a single chunk is a FAILURE, not an empty
    # answer, and it has to be flagged as one - a provider that times out or
    # drops the connection, and one that answered with nothing, look identical
    # here, and the flag is what sends the caller to the batch path for the
    # real reason.
    text = _strip_control_tags(text)
    # Rough: providers report real counts only on the batch path, and a stream
    # gives none. ~4 chars/token is the same estimate the compaction code uses.
    return {"response": text, "tokens": len(text) // 4}


async def _stream_round(provider, messages: list[dict], model, only,
                        reply_directive: str):
    """Run one loop round as a stream, returning it only if it is a plain answer.

    Returns the round result with `streamed: True` when the round produced text
    and no tool call - the case where streaming is correct and the text is the
    answer. Returns None when the round looks like it wants a tool, or the
    stream failed, so the caller falls back to the batch call.

    The text is NOT emitted here. A stream on this path can carry a tool call
    written as text (that is how a provider without native function calling
    works), and printing that at the user and then erasing it is worse than not
    streaming at all. So the decision comes first and the emission second - see
    the `on_delta` call the caller makes once this returns.

    Only text-only rounds are accepted. Anything else returns None, which costs
    one batch call and preserves the previous behaviour exactly.
    """
    try:
        streamed = await _streamed_answer(provider, messages, model,
                                          on_delta=None)
    except Exception as e:  # noqa: BLE001
        log.debug("stream round failed, falling back: %s", e)
        return None
    if streamed.get("stream_failed"):
        return None
    # A native tool call was seen in the stream: this round is a TOOL round, so
    # its text is narration, not the answer, and it must go back to the batch
    # path where the call can actually be read and executed. This is the check
    # the text parsers below cannot make - 9router/Voxagent streams
    # `content="I'll check that."` alongside a real `tool_calls` array, so the
    # text alone reads as an answer and the call was lost.
    if streamed.get("stream_tool_call"):
        return None
    text = streamed.get("response") or ""
    if not text.strip():
        return None
    # Either call syntax means this round is a tool round, not an answer.
    if _parse_tool_response(text)["calls"] or _extract_tool_calls(text):
        return None
    return {"response": text, "tokens": streamed.get("tokens", 0),
            "streamed": True}


async def _call_native_tools(provider, messages: list[dict],
                             model: str | None = None,
                             only: set[str] | None = None,
                             reply_directive: str = "",
                             on_delta=None,
                             final: bool = False) -> dict:
    """Use native function-calling API (OpenAI/DeepSeek/Gemini)."""
    from backend.providers import system_role
    tools = skill_registry.to_openai_tools(only)
    messages = _with_directive(messages, reply_directive)

    # A final round is text by construction: _final_answer has already told the
    # model to stop calling tools. Streaming it is safe, and it is the only round
    # worth streaming - every earlier round is narration the model may discard.
    # Tool rounds stay on the batch call; see _streamed_answer for why.
    # `on_delta is not None` is part of the test, not a detail. Streaming is a
    # UI affordance, and a caller with no callback (a bot, a check, the Code
    # page's own planner) must keep the byte-for-byte batch path it had before:
    # otherwise every headless caller silently changes its provider call, its
    # token accounting and its error handling the moment this ships.
    if final and on_delta is not None:
        streamed = await _streamed_answer(provider, messages, model,
                                          on_delta=on_delta)
        # A tool call in a round that was TOLD to answer in plain text: the
        # model ignored the instruction. Falling through to the batch call
        # keeps the call rather than emitting narration that claims work which
        # never happened - the same loss, one path over.
        if not streamed.get("stream_failed") \
                and not streamed.get("stream_tool_call"):
            return streamed

    try:
        try:
            result = await provider.chat(
                messages,
                model=model,
                max_tokens=4096,
                temperature=0.7,
                tools=tools,
            )
        except TypeError:
            # Provider doesn't accept a tools kwarg → prompt-injected tools
            return await _call_prompt_tools(provider, messages, model, only,
                                            reply_directive)

        # A round that FAILED but still parsed tool calls must not have them
        # silently dropped. That ordering was load-bearing in a real outage:
        # `openai_provider` tested `not content.strip()` before reading
        # `tool_calls`, so it returned ok=False for a model that had sent a
        # perfectly good call, and this branch then discarded whatever else
        # the round carried. The provider is fixed (see check_tool_calls.py);
        # this is the loop refusing to lose a call on the same shape, so a
        # different provider cannot reintroduce it.
        # A round that FAILED but still parsed tool calls must not have them
        # silently dropped. That ordering was load-bearing in a real outage:
        # `openai_provider` tested `not content.strip()` before reading
        # `tool_calls`, so it returned ok=False for a model that had sent a
        # perfectly good call, and this branch then discarded whatever else
        # the round carried. The provider is fixed (see check_tool_calls.py);
        # this is the loop refusing to lose a call on the same shape, so a
        # different provider cannot reintroduce it.
        if not result.ok and not getattr(result, "tool_calls", None):
            err_lower = (result.error or "").lower()
            if any(k in err_lower for k in ("tool", "function", "unrecognized field", "extra fields", "not supported")):
                log.info("Native tools unsupported by provider '%s' (%s), falling back to prompt tools",
                         getattr(provider, "provider_id", "?"), result.error)
                return await _call_prompt_tools(provider, messages, model, only,
                                                reply_directive)
            log.warning("Provider '%s' failed: %s",
                        getattr(provider, "provider_id", "?"), result.error)
            return {"response": f"[Provider error: {result.error}]", "tokens": 0}

        response_text = result.response or ""
        tokens = result.tokens_in + result.tokens_out

        # Learn from the token count whether this endpoint actually sent the
        # system prompt. Free — the usage is already in the response — and it
        # decides whether the next turn needs the fold-in above.
        try:
            system_role.record(provider, result.tokens_in, messages)
        except Exception as e:  # noqa: BLE001
            log.debug("system-role detection failed: %s", e)

        # Parse native tool calls (OpenAI format)
        tool_calls = []
        if result.tool_calls:
            for tc in result.tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                if not name:
                    continue
                try:
                    params = json.loads(fn.get("arguments", "{}") or "{}")
                except json.JSONDecodeError:
                    params = {}
                tool_calls.append({"name": name, "params": params, "id": tc.get("id")})
        if tool_calls:
            return {
                "response": response_text,
                "tokens": tokens,
                "tool_calls": tool_calls,
                "raw_tool_calls": result.tool_calls,
                "reasoning_content": getattr(result, "reasoning_content", ""),
            }

        # Fallback: models that output ```tool blocks in plain text
        text_calls = _extract_tool_calls(response_text)
        if text_calls:
            return {
                "response": response_text,
                "tokens": tokens,
                "tool_calls": text_calls,
            }

        return {"response": response_text, "tokens": tokens}

    except Exception as e:
        log.warning("Native tool call failed: %s", e)
        return {"response": f"Error: {e}", "tokens": 0}


async def _call_prompt_tools(provider, messages: list[dict],
                             model: str | None = None,
                             only: set[str] | None = None,
                             reply_directive: str = "",
                             on_delta=None,
                             final: bool = False) -> dict:
    """For providers without native tool support: give it the catalogue first.

    The catalogue is its own message immediately before the user's, rather than
    appended to it. Glued on, a twenty-character question became ~6,600
    characters of English tool instructions with the question buried at the top,
    and the model then treated its own previous answer as the thing to continue:
    asked "cukup untuk sekarang" right after a 42-item list, it wrote the list
    out again (three samples: 516, 564 and 1920 characters, up to 32 items
    repeated). Separated, the same turn answers in ~200 characters and repeats
    nothing. The question is also what the model reads last again, which is where
    a small model looks to find out what it was asked.
    """
    from backend.providers import system_role
    if only is None:
        query = _last_user_text(messages)
        only = skill_registry.filter_for_query(query)

    tools_text = skill_registry.to_prompt_tools(only)

    modified_messages = list(messages)
    if modified_messages and modified_messages[-1]["role"] == "user":
        question = modified_messages.pop()
        if tools_text:
            modified_messages.append({"role": "user", "content": tools_text})
        modified_messages.append(question)

    # A tool result decides the next step, so it must be answered with the
    # catalogue still in hand. Without this the only instruction the model saw
    # was the unrelated user question from before, and the turn could come back
    # as a plain answer instead of the follow-up call the result asked for.
    if modified_messages and modified_messages[-1].get("role") == "tool" \
            and tools_text:
        modified_messages.append({"role": "user", "content": tools_text})

    # After the catalogue, not before it: the catalogue is the bulk of what the
    # model reads before answering, so a language rule placed earlier is what it
    # overrides (see backend/language.py). It rides the last message, which is
    # the user's question again.
    modified_messages = _with_directive(modified_messages, reply_directive)
    # The call format is repeated on the question, because one message away is
    # far enough for a small model to forget it is allowed to call anything:
    # asked five questions that need a tool, the same turn called one 4 times out
    # of 10 with the format only in the catalogue and 10 out of 10 with it here —
    # while the questions that need no tool were unaffected either way.
    if skill_registry.prompt_tool_count(only) \
            and modified_messages[-1].get("role") == "user":
        last = modified_messages[-1]
        modified_messages[-1] = {
            **last,
            "content": (last.get("content") or "") + "\n\n"
                       + skill_registry.PROMPT_CALL_FORMAT,
        }

    # Everything above plus the catalogue has to fit the provider's window.
    # The local model's context is 8192, so an unbudgeted request is rejected
    # outright — which is what made tool use fail there and nowhere else.
    provider_id = getattr(provider, "provider_id", "") or ""
    limit = budget.context_limit(provider_id)
    wanted = int(provider_id == "local" and 1536 or 4096)
    prompt_tokens = budget.message_tokens(modified_messages)
    trimmed = 0
    if prompt_tokens + budget.MIN_REPLY_TOKENS > limit:
        # Drop older middle turns, then re-measure with the catalogue in place.
        modified_messages, trimmed = budget.fit_messages(
            modified_messages, limit, budget.MIN_REPLY_TOKENS)
        if trimmed:
            log.info("Trimmed %d older message(s) to fit %s's %d-token "
                     "context", trimmed, provider_id or "provider", limit)
        prompt_tokens = budget.message_tokens(modified_messages)
    max_tokens = budget.reply_budget(prompt_tokens, limit, want=wanted)
    if max_tokens <= 0:
        return {"response": ("This request is larger than %s can hold (about "
                             "%d tokens of context). Try a shorter question, "
                             "or switch provider."
                             % (provider_id or "the model", limit)),
                "tokens": 0}

    # The stream is buffered, never shown as it arrives, because unlike a native
    # round this one CAN carry a tool call - the whole point of this path is that
    # the model writes its call as text. Streaming a call to the screen would
    # print JSON at the user and then erase it. So: collect the deltas, and only
    # release them once the text is known to contain no call. `on_delta` is not
    # wired until that decision, which is why the buffer is kept at all.
    stream_text = ""
    if final and on_delta is not None:
        streamed = await _streamed_answer(provider, modified_messages, model,
                                          max_tokens=max_tokens)
        if not streamed.get("stream_failed") and not streamed.get("stream_tool_call"):
            stream_text = streamed.get("response") or ""
            if not _parse_tool_response(stream_text)["calls"] \
                    and not _extract_tool_calls(stream_text):
                try:
                    on_delta(stream_text)
                except Exception:
                    log.debug("stream listener failed", exc_info=True)
                return {"response": stream_text,
                        "tokens": streamed.get("tokens", 0)}
            stream_text = ""  # it was a tool call; fall through to the batch path
        else:
            stream_text = ""  # a native tool call, or the stream failed; batch path

    try:
        result = await provider.chat(
            modified_messages,
            model=model,
            max_tokens=max_tokens,
            temperature=0.7,
        )

        if not result.ok:
            log.warning("Provider '%s' failed (prompt tools): %s",
                        getattr(provider, "provider_id", "?"), result.error)
            return {"response": f"[Provider error: {result.error}]", "tokens": 0}

        # Learn from the token count whether this endpoint actually sent the
        # system prompt (see backend/providers/system_role.py). This path
        # matters as much as the native one: a provider id outside
        # NATIVE_TOOL_PROVIDERS — 9router, say — comes here for EVERY turn, so
        # detecting only on the native path would never fire at all.
        try:
            system_role.record(provider, result.tokens_in, modified_messages)
        except Exception as e:  # noqa: BLE001
            log.debug("system-role detection failed: %s", e)

        response_text = result.response
        tokens = result.tokens_in + result.tokens_out

        parsed = _parse_tool_response(response_text)
        tool_calls = parsed["calls"]

        # Only strip the blocks that were actually tool calls, so any code the
        # model legitimately showed the user survives.
        visible_response = response_text
        for block in parsed["blocks"]:
            visible_response = visible_response.replace(block, "")
        visible_response = _strip_control_tags(visible_response).strip()

        return {
            # Falling back to `response_text` only when NOTHING is left, which
            # is the case where the reply was the call and nothing else. The
            # control tags are removed on both sides of it, so the fallback can
            # never hand back the tag this just stripped.
            "response": visible_response or _strip_control_tags(response_text),
            "tokens": tokens,
            "tool_calls": tool_calls,
            "malformed": parsed["malformed"],
        }

    except Exception as e:
        log.warning("Prompt-based tool call failed: %s", e)
        return {"response": f"Error: {e}", "tokens": 0}


def _json_objects(text: str):
    r"""Yield every balanced-brace JSON object in the text.

    A regex cannot do this: ``\{[^}]*\}`` stops at the first closing brace, so
    it never matched a call with parameters — which is all of them. This walks
    the text and respects nesting and string escapes.
    """
    depth = 0
    start = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start:index + 1]
                start = None


def _normalise_tool_name(name) -> str:
    """The skill a model meant, from the name it actually wrote.

    Most of the time the name is exactly right. But the catalogue shows a tool
    as `name?arg`, a model will sometimes answer with `get_screen_size()` —
    parentheses, and occasionally an argument list, quotes or a trailing stop.
    None of that is part of a skill name, and an unmatched name is expensive
    rather than merely wrong: it reads as a *missing* skill, so the whole
    market-and-forge path runs (writing a generated file under a nonsense
    name) before the user is told the tool does not exist.

    Skill names never contain whitespace, so the first word is the name.
    """
    cleaned = str(name or "").strip().strip('"\'`')
    cleaned = cleaned.split("(", 1)[0]
    cleaned = cleaned.split()[0] if cleaned.split() else ""
    return cleaned.rstrip(".,;:`")


def _as_tool_call(candidate: dict) -> dict | None:
    """Normalise the several shapes models use for a tool call."""
    if not isinstance(candidate, dict):
        return None
    name = candidate.get("tool") or candidate.get("name")
    if not name and isinstance(candidate.get("function"), dict):
        name = candidate["function"].get("name")
        args = candidate["function"].get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        cleaned = _normalise_tool_name(name)
        if not cleaned:
            return None
        return {"name": cleaned, "params": args or {}}
    if not name:
        return None
    params = candidate.get("params") or candidate.get("parameters") \
        or candidate.get("arguments") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError:
            params = {}
    cleaned = _normalise_tool_name(name)
    if not cleaned:
        return None
    return {"name": cleaned, "params": params if isinstance(params, dict)
            else {}}


def _extract_tool_calls(text: str) -> list[dict]:
    """The tool calls in a piece of text. Kept list-shaped: the native path
    falls back to this and treats the result as a list."""
    return _parse_tool_response(text)["calls"]


# `name("argument")`, `name(key=value, ...)` — a Python-shaped call, which is
# what the catalogue's own `name(arg)` rendering invites a model to write.
_CALL_SYNTAX = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\((.*)\)\s*$",
                          re.DOTALL)


def _declared_params(name: str) -> list[str]:
    """The parameter names a skill declares, in the order it declares them.

    A positional argument needs this: `list_dir("E:\\x")` means the first
    parameter, and only the schema says that it is called `path`.
    """
    try:
        skill = skill_registry.get(_normalise_tool_name(name))
        properties = (skill.parameters or {}).get("properties") or {}
        return [str(key) for key in properties]
    except Exception:  # noqa: BLE001
        return []


def _split_args(text: str) -> list[str]:
    """Split an argument list on the commas outside quotes and brackets."""
    parts: list[str] = []
    current = ""
    depth = 0
    quote = ""
    escaped = False
    for char in text:
        if escaped:
            current += char
            escaped = False
            continue
        if char == "\\":
            current += char
            escaped = True
            continue
        if quote:
            current += char
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            current += char
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth <= 0:
            parts.append(current)
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def _literal(text: str):
    """A written argument as a value: JSON when it parses, else the text."""
    raw = text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _call_from_syntax(text: str) -> dict | None:
    """A call written as `name("arg")` / `name(key=value)` rather than JSON.

    Models write this often — it is how a Python-shaped call looks, and the
    catalogue prints each tool as `name(arguments)`. It matters more than a
    missed call usually would: with nothing parsed there is no tool round, so
    the raw text becomes the answer. That is how "I have created the file"
    reached a user while nothing had run, and how four replies in a row were
    the model asking to list a directory and getting no answer from anybody.
    """
    match = _CALL_SYNTAX.match(str(text or "").strip())
    if not match:
        return None
    name = _normalise_tool_name(match.group(1))
    if not name:
        return None
    if not skill_registry.get(name):
        # Only a real skill. Call syntax is a guess at what the model meant, and
        # an unknown name there would otherwise start the market-and-forge path
        # — writing a generated skill file — on the strength of a guess.
        return None
    inner = match.group(2).strip()
    if not inner:
        return {"name": name, "params": {}}
    if inner.startswith("{"):
        try:
            as_json = json.loads(inner)
        except json.JSONDecodeError:
            as_json = None
        if isinstance(as_json, dict):
            return {"name": name, "params": as_json}
    declared = _declared_params(name)
    params: dict = {}
    position = 0
    for part in _split_args(inner):
        key, separator, value = part.partition("=")
        if separator and declared \
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            params[key.strip()] = _literal(value)
            continue
        params[declared[position] if position < len(declared)
               else f"arg{position + 1}"] = _literal(part)
        position += 1
    return {"name": name, "params": params}


# `<name><param>value</param></name>` — the XML-ish shape several models emit
# regardless of what the catalogue asks for.
#
# 9router + Voxagent is one: told in plain terms to answer with a fenced
# ```tool JSON block, it replied "I can't emit a call for a tool I don't have"
# and wrote `<run_command><command>dir</command></run_command>` anyway. The
# format it uses is its own habit, not a mistake to correct — and the old
# parser returned nothing at all for it, not even `malformed`, so the call was
# invisible and the turn became a plain answer. Meeting the model where it is
# costs one regex; not doing so costs every tool call it ever attempts.
_XML_CALL = re.compile(
    r"<([A-Za-z_][A-Za-z0-9_.-]*)\s*>(.*?)</\1>", re.DOTALL)
_XML_PARAM = re.compile(
    r"<([A-Za-z_][A-Za-z0-9_.-]*)\s*>(.*?)</\1>", re.DOTALL)


# DeepSeek's DSML markup, emitted as literal TEXT rather than through the API's
# `tool_calls` field. Caught live: the model answered a question about a saved
# meeting with
#
#     <｜｜DSML｜｜ calls>
#     <｜｜DSML｜｜ invoke name="meeting_list">
#     </｜｜DSML｜｜ invoke>
#     </｜｜DSML｜｜ calls>
#
# and nothing else. The old parser returned `[]` for that AND did not mark it
# malformed, so a real call was discarded in silence: no tool ran, and the user
# was shown the raw markup as if it were prose.
#
# Both the fullwidth bars (U+FF5C, what was actually emitted) and the ASCII
# pipes are accepted, because which one appears depends on the build and
# guessing wrong costs the whole call. Sibling of `_call_from_xml` and here for
# the same reason it gives: the format is the model's habit, not a mistake, and
# meeting it costs one regex.
_DSML_BARS = r"(?:\uff5c|\|)"
_DSML_CALL = re.compile(
    r"<" + _DSML_BARS + r"*DSML" + _DSML_BARS + r"*\s+invoke\s+name\s*=\s*"
    r"[\"\']?([A-Za-z_][A-Za-z0-9_.-]*)[\"\']?\s*>(.*?)</"
    + _DSML_BARS + r"*DSML" + _DSML_BARS + r"*\s+invoke\s*>",
    re.DOTALL | re.IGNORECASE)
_DSML_PARAM = re.compile(
    r"<" + _DSML_BARS + r"*DSML" + _DSML_BARS + r"*\s+parameter\s+name\s*=\s*"
    r"[\"\']?([A-Za-z_][A-Za-z0-9_.-]*)[\"\']?[^>]*>(.*?)</"
    + _DSML_BARS + r"*DSML" + _DSML_BARS + r"*\s+parameter\s*>",
    re.DOTALL | re.IGNORECASE)


# A plain fence whose body is a real command. The model's habit when it does not
# use the JSON the catalogue asks for, and invisible until now: the fence parsed
# as nothing, was not flagged malformed, and the command was shown to the user
# as prose while nothing ran.
#
# Deliberately strict. A fence is also how a model SHOWS a command it is not
# running ("you would run this:"), and folding every one would execute things
# the user was only being shown. So the first word must be a program or a known
# PowerShell cmdlet, and the body must be a command line rather than prose.
_SHELL_FIRST_WORDS = frozenset({
    # Shells and interpreters
    "cmd", "powershell", "pwsh", "bash", "sh", "zsh", "python", "python3",
    "node", "deno", "bun", "ruby", "perl", "php", "dotnet", "java",
    # Package and build tools
    "pip", "pip3", "npm", "npx", "pnpm", "yarn", "uv", "uvx", "poetry",
    "cargo", "rustc", "go", "make", "cmake", "gradle", "mvn",
    "winget", "choco", "scoop", "git", "gh", "docker", "kubectl", "terraform",
    # Core Windows cmdlets and console tools - what this machine's transcripts
    # actually contain.
    "get-childitem", "get-item", "get-itemproperty", "get-command",
    "get-process", "get-service", "get-content", "get-location", "get-date",
    "get-help", "get-member", "get-module", "get-psdrive", "get-volume",
    "set-location", "set-item", "set-itemproperty", "set-content",
    "new-item", "new-itemproperty", "remove-item", "copy-item", "move-item",
    "rename-item", "select-object", "where-object", "sort-object",
    "format-table", "format-list", "measure-object", "select-string",
    "start-process", "stop-process", "invoke-item", "invoke-webrequest",
    "invoke-restmethod", "test-path", "test-connection", "resolve-path",
    "join-path", "split-path", "convertto-json", "convertfrom-json",
    "import-module", "write-output", "write-host", "out-file", "out-string",
    "measure-command", "start-sleep", "tasklist", "taskkill", "dir", "ls",
    "cat", "type", "echo", "where", "which", "whoami", "hostname", "ipconfig",
    "netstat", "ping", "curl", "wget", "tree", "cls", "clear", "date", "set",
    "reg", "sc", "net", "wmic", "systeminfo", "robocopy", "xcopy", "attrib",
})


def _call_from_shell_fence(text: str) -> dict | None:
    """Read a plain fenced block that holds a real command as `run_command`.

    Returns None unless the whole block is a command. Returning a call for
    something that was only being explained would RUN it, which is the failure
    mode worth being strict about - the opposite mistake (missing one) leaves
    the reply as an answer, which is where it already was.
    """
    if not text or "```" not in text:
        return None
    for match in re.finditer(
            r"```([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)(?:```|\Z)",
            text, re.DOTALL):
        language = (match.group(1) or "").lower()
        # A tagged fence already went through the format readers above; only an
        # untagged (or explicitly shell-tagged) block is a candidate here.
        if language not in ("", "ps1", "powershell", "shell", "sh", "bash",
                            "cmd", "bat"):
            continue
        body = match.group(2).strip()
        if not body or len(body) > 4000:
            continue
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        if not lines or len(lines) > 40:
            continue
        # A single bare verb is a name being mentioned, not a command being run:
        # "the tool is `dir`" and "run `dir`" are the same two characters.
        # Requiring an argument or a second line keeps the reader to the shape
        # that actually means act.
        if len(lines) == 1 and len(lines[0].split()) < 2:
            continue
        # A shell fence routinely OPENS with a comment, and the model writes
        # them habitually - measured live on 2026-10-10, the whisper check came
        # back as:
        #
        #     ```bash
        #     # Check for whisper CLI command
        #     which whisper
        #     ```
        #
        # Rejecting on the first line therefore discarded a real command and
        # showed the fence to the user as the answer, with nothing run. So the
        # first *command* line is what the checks below read: leading comments
        # (and blanks) are stepped over, and only a body that is ALL comment
        # falls out. The rejection rules themselves are unchanged and still
        # apply to the line that actually carries the command.
        command_lines = [ln for ln in lines if not ln.startswith("#")]
        if not command_lines:
            continue
        first = command_lines[0]
        # Plainly an answer rather than a command: JSON, a quoted string, a
        # sentence. (A bare comment is handled above; a first line that is
        # prose is still refused here.)
        if first.startswith(("{", "[", "\"", "'")):
            continue
        head = re.split(r"[\s|;&]+", first, 1)[0].strip().lower().lstrip("-")
        if head not in _SHELL_FIRST_WORDS:
            continue
        # Only a real skill, the rule every reader here follows: an unknown name
        # starts the market-and-forge path and writes a generated file under a
        # name the model invented.
        name = _normalise_tool_name("run_command")
        if not name or not skill_registry.get(name):
            return None
        declared = _declared_params(name)
        return {"name": name,
                "params": {(declared[0] if declared else "command"): body}}
    return None


# `<tool_call>` wrappers, seen live as `<tool_call>meeting_list</tool_call>`.
# The JSON form inside the tag already parsed (the fenced-json path finds the
# object); the bare-name and attribute forms did NOT, and -- like DSML -- they
# were not marked malformed either, so a real call vanished and the tag was
# shown to the user as prose.
_TOOLCALL_TAG = re.compile(
    r"<tool_call\b[^>]*>(.*?)</tool_call\s*>", re.DOTALL | re.IGNORECASE)
_TOOLCALL_SELF = re.compile(
    r"<tool_call\b[^>]*\bname\s*=\s*[\"\']([A-Za-z_][A-Za-z0-9_.-]*)[\"\'][^>]*/?>",
    re.IGNORECASE)


# Control tags the model invents and wraps its own bookkeeping in. Caught live
# on 2026-10-10 in a reply that admitted the search never ran:
#
#     "... nothing has been searched.
#      Let me run it properly now.
#      <budget:token_budget>2000</budget:token_budget>"
#
# `budget:` appears nowhere in this codebase -- the model writes it from
# training, the same way it writes DSML and `<run_command>`. Nothing consumed
# it and nothing removed it, so it was stored in the history and shown to the
# user as part of the answer.
#
# The shape is a colon-namespaced element, which is what makes this safe to do
# broadly: `<budget:token_budget>` cannot be mistaken for content, while a URL
# (`<http://...>`), a comparison (`a < b`) and a real control-flow tag from a
# language the user asked for (`<div:class>`) are all left alone. Only the
# namespaced element is removed, and its body with it -- an empty pair left
# behind would be a worse artifact than the tag.
_CTRL_ELEMENT = re.compile(
    r"<([A-Za-z_][A-Za-z0-9_.-]*):([A-Za-z_][A-Za-z0-9_.-]*)\b[^>]*>"
    r".*?</\1:\2\s*>", re.DOTALL)
_CTRL_TAG = re.compile(
    r"</?[A-Za-z_][A-Za-z0-9_.-]*:[A-Za-z_][A-Za-z0-9_.-]*\b[^>]*/?>")


def _strip_control_tags(text: str) -> str:
    """Remove the model's own control tags from anything the user will read."""
    if not text or ":" not in text or "<" not in text:
        return text
    cleaned = _CTRL_TAG.sub("", _CTRL_ELEMENT.sub("", text))
    return cleaned if cleaned != text else text


# `` `name`: `argument` `` -- the model's markdown habit.
#
# Caught on the live install (2026-10-10). Asked to check for ffmpeg, this model
# answered "`run_command`: `ffmpeg -version`" and nothing ran: the shape was
# unknown, so `_parse_tool_response` reported neither a call nor a malformed one
# and the reply became a plain answer. The user was told to click Allow on a
# card that had never been raised.
#
# Anchored to the start of a line, and to a real skill name, so prose that
# mentions a tool in backticks is not mistaken for a call.
_MARKDOWN_CALL = re.compile(
    r"^\s*`([A-Za-z_][A-Za-z0-9_.-]*)`\s*:\s*`([^`]*)`\s*$", re.MULTILINE)

# Deliberately LOOSER than the parser above: the same opening shape with extra
# text on the line, which the parser refuses to bind but must not ignore. Used
# only to report a call attempt as unreadable, never to run anything.
_MARKDOWN_MAYBE = re.compile(
    r"^\s*`([A-Za-z_][A-Za-z0-9_.-]*)`\s*:", re.MULTILINE)

def _call_from_markdown(text: str) -> dict | None:
    """A call written `` `name`: `argument` ``, the model's own markdown habit.

    Only a real skill is accepted, for the reason `_call_from_syntax` gives: an
    unknown name would start the market-and-forge path on the strength of a
    guess, writing a generated skill file under a nonsense name.

    The single backticked argument is bound to the skill's declared parameter --
    usually `command` -- so `run_command` receives `{"command": "ffmpeg -version"}`
    rather than nothing.
    """
    match = _MARKDOWN_CALL.search(str(text or ""))
    if not match:
        return None
    name = _normalise_tool_name(match.group(1))
    if not name:
        return None
    if not skill_registry.get(name):
        return None
    argument = match.group(2).strip()
    if not argument:
        return {"name": name, "params": {}}
    declared = _declared_params(name)
    if declared:
        # The skill names its parameters; bind the one argument to the first.
        return {"name": name, "params": {declared[0]: _literal(argument)}}
    return {"name": name, "params": {"command": _literal(argument)}}

def _call_from_toolcall_tag(text: str) -> dict | None:
    """A call wrapped in `<tool_call>` tags, naming the tool bare.

    Only a real skill name is accepted, the same rule `_call_from_xml` and
    `_call_from_dsml` follow: an unmatched name starts the market-and-forge
    path and writes a generated file under a name the model invented.
    """
    inner = str(text or "").strip()
    if "tool_call" not in inner.lower():
        return None
    for match in _TOOLCALL_TAG.finditer(inner):
        body = match.group(1).strip()
        if not body:
            continue
        # `name(arg)` or a bare name. JSON inside the tag is handled earlier by
        # the object path, so a body starting with '{' is not this parser's job.
        candidate = body
        if "(" in candidate:
            candidate = candidate.split("(", 1)[0]
        candidate = candidate.split()[0] if candidate.split() else ""
        name = _normalise_tool_name(candidate)
        if name and skill_registry.get(name):
            return {"name": name, "params": {}}
    for match in _TOOLCALL_SELF.finditer(inner):
        name = _normalise_tool_name(match.group(1))
        if name and skill_registry.get(name):
            return {"name": name, "params": {}}
    return None


def _call_from_dsml(text: str) -> dict | None:
    """The FIRST DSML call in `text`, or None.

    Deliberately mirrors `_call_from_xml`: only a real skill name is accepted,
    because an angle-bracket tag is a guess at what the model meant and a wrong
    guess starts the market-and-forge path.
    """
    inner = str(text or "").strip()
    if "DSML" not in inner:
        return None
    for match in _DSML_CALL.finditer(inner):
        name = _normalise_tool_name(match.group(1))
        if not name or not skill_registry.get(name):
            continue
        params: dict = {}
        for pm in _DSML_PARAM.finditer(match.group(2)):
            params[pm.group(1).strip()] = pm.group(2).strip()
        return {"name": name, "params": params}
    return None


def _call_from_xml(text: str) -> dict | None:
    """A call written as `<name><param>value</param></name>`.

    Only a real skill name: an angle-bracket tag is a guess at what the model
    meant, and guessing wrong starts the market-and-forge path, which writes a
    generated skill file. The same rule `_call_from_syntax` follows.
    """
    inner = str(text or "").strip()
    if not inner or "<" not in inner:
        return None
    for match in _XML_CALL.finditer(inner):
        name = _normalise_tool_name(match.group(1))
        if not name or not skill_registry.get(name):
            continue
        body = match.group(2)
        params: dict = {}
        for pm in _XML_PARAM.finditer(body):
            key = pm.group(1).strip()
            if key == match.group(1):
                continue          # the outer tag, not a parameter
            params[key] = pm.group(2).strip()
        if not params:
            # `<name>bare value</name>`: a single unnamed argument.
            bare = body.strip()
            if bare and "<" not in bare:
                declared = _declared_params(name)
                params[declared[0] if declared else "arg1"] = bare
        if params:
            return {"name": name, "params": params}
        # A no-argument call: <session_list></session_list>
        return {"name": name, "params": {}}
    return None


def _looks_like_call(text: str) -> bool:
    """Whether this is a tool call that could not be read.

    Deliberately narrow: a ```python block in an answer is not a call, and a
    name that is not a skill is prose. Only a real skill name in call syntax,
    or JSON that names a tool and did not parse, counts.
    """
    inner = str(text or "").strip()
    if not inner:
        return False
    if '"tool"' in inner or '"name"' in inner:
        return True
    match = _CALL_SYNTAX.match(inner)
    if bool(match) and bool(
            skill_registry.get(_normalise_tool_name(match.group(1)))):
        return True
    # An angle-bracket tag naming a real skill, that the XML parser could not
    # read. Reporting it as malformed is the point: the silent version of this
    # is what let a tool call become a plain answer with no error anywhere.
    for m in re.finditer(r"<([A-Za-z_][A-Za-z0-9_.-]*)[\s>]", inner):
        if skill_registry.get(_normalise_tool_name(m.group(1))):
            return True
    # `` `name`: `...` `` naming a real skill. Same reason as the tag above: if
    # the markdown reader could not parse it, it must be reported so one
    # corrective round is offered -- silently answering in prose is what the
    # live install did, and the user saw a permission prompt that never existed.
    for m in _MARKDOWN_MAYBE.finditer(inner):
        if skill_registry.get(_normalise_tool_name(m.group(1))):
            return True
    return False


def _parse_tool_response(text: str) -> dict:
    """Find tool calls, the blocks that should not be shown, and the ones that
    could not be read.

    Accepts ```tool, ```json or an unfenced object, on one line or several, and
    a call written as `name("arg")`, so a small model is not required to match
    one exact format. Returns ``{"calls": [...], "blocks": [...],
    "malformed": [...]}``.
    """
    if not text:
        return {"calls": [], "blocks": [], "malformed": []}

    calls: list[dict] = []
    blocks: list[str] = []
    malformed: list[str] = []

    # The closing fence is optional on purpose: a reply cut off at the token cap
    # has no closing fence, and that is exactly the case that used to slip
    # through as an answer.
    for match in re.finditer(r"```([A-Za-z]*)[ \t]*\r?\n?(.*?)(?:```|\Z)",
                             text, re.DOTALL):
        language = (match.group(1) or "").lower()
        inner = match.group(2).strip()
        if not inner:
            continue
        found = False
        for candidate in _json_objects(inner):
            try:
                call = _as_tool_call(json.loads(candidate))
            except json.JSONDecodeError:
                continue
            if call:
                calls.append(call)
                found = True
        if not found and language in ("", "tool", "json"):
            call = _call_from_syntax(inner)
            if call:
                calls.append(call)
                found = True
        if not found and language in ("", "tool", "json", "xml"):
            call = _call_from_xml(inner)
            if call:
                calls.append(call)
                found = True
        if found:
            blocks.append(match.group(0))
        elif _looks_like_call(inner):
            malformed.append(inner)

    if not calls:
        # No fenced block, or nothing usable in it: look at the raw text. A
        # tool block left visible to the user is worse than a strict parse.
        for candidate in _json_objects(text):
            try:
                call = _as_tool_call(json.loads(candidate))
            except json.JSONDecodeError:
                continue
            if call:
                calls.append(call)
                blocks.append(candidate)
        if not calls:
            bare = _call_from_syntax(text)
            if bare:
                calls.append(bare)
                blocks.append(text.strip())
        if not calls:
            # The model's markdown habit, before the tag readers: it is the one
            # shape that has no tag to catch, and the one that was being lost
            # in silence.
            md_call = _call_from_markdown(text)
            if md_call:
                calls.append(md_call)
                blocks.append(text.strip())
        if not calls:
            # A plain fence holding a real command: the shape the user's Flux
            # search was written in, and the one that ran nothing.
            fence_call = _call_from_shell_fence(text)
            if fence_call:
                calls.append(fence_call)
                blocks.append(text.strip())
        if not calls:
            # DSML before the generic XML form: a DSML reply also parses as
            # nothing under `_call_from_xml` (its tags are `<|DSML| invoke>`,
            # not `<name>`), so order does not change the result for DSML --
            # but a DSML block that DID match the XML regex would otherwise be
            # read as a tool called "DSML", which is not a skill.
            dsml_call = _call_from_dsml(text)
            if dsml_call:
                calls.append(dsml_call)
                blocks.append(text.strip())
        if not calls:
            # `<tool_call>meeting_list</tool_call>`. The JSON form inside this
            # tag was already handled above; the bare-name form was not, and
            # like DSML it was discarded silently rather than flagged.
            tagged = _call_from_toolcall_tag(text)
            if tagged:
                calls.append(tagged)
                blocks.append(text.strip())
        if not calls:
            xml_call = _call_from_xml(text)
            if xml_call:
                calls.append(xml_call)
                # The whole reply is swallowed as a block: the model wrapped the
                # call in its prose ("Let me do it."), and showing that plus the
                # markup would leak the call to the user.
                blocks.append(text.strip())
        if not calls and not malformed and _looks_like_call(text):
            # Nothing parsed, and the reply still looks like a call. Reported so
            # one corrective round is offered. Before this, `malformed` was only
            # ever filled inside the fenced-block loop above, so a call written
            # as bare text was invisible -- the user's 2026-10-10 reply had no
            # fence, and the call was silently dropped into the answer.
            malformed.append(str(text).strip())

    return {"calls": calls, "blocks": blocks, "malformed": malformed}


def _tool_result_message(tool_name: str, result: dict, tool_call_id: str | None = None) -> dict:
    """Format a tool/forge result as a message for the provider."""
    if result.get("success"):
        summary = json.dumps(result.get("data", {}), indent=2)[:3000]
        forged_note = " (newly forged!)" if result.get("forged") else ""
        content = f"Tool '{tool_name}' succeeded{forged_note}.\nResult:\n{summary}"
    else:
        content = f"Tool '{tool_name}' failed.\nError: {result.get('error', 'unknown')}"
        # A refusal is an instruction, not a dead end. `execute_skill` puts the
        # approval hint in `data` and drops the rest of the payload on the way
        # out, so a message carried anywhere else never reaches the model — and
        # the model then reports the gate and stops instead of retrying with
        # confirm=true.
        candidates = [result, result.get("data"), result.get("result"),
                      (result.get("data") or {}).get("data")]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if not candidate.get("message"):
                continue
            if candidate.get("message") in content:
                continue
            content += f"\n{candidate['message']}"
            break
    msg: dict = {"role": "tool", "content": content}
    if tool_call_id:
        msg["tool_call_id"] = tool_call_id
    return msg
