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


async def execute_skill(name: str, params: dict, provider=None) -> dict:
    """Execute a skill by name, recording usage/failure telemetry."""
    result = await _execute_skill_inner(name, params, provider)
    try:
        from backend.skills.telemetry import record
        record(name, bool(result.get("success")))
    except Exception:
        pass
    return result


async def _execute_skill_inner(name: str, params: dict, provider=None) -> dict:
    """
    Execute a skill by name. If not found, try the forge.
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

    # Skill not found — try the market first, then the forge
    log.info("Skill '%s' not found — attempting market search", name)
    try:
        from backend.config import config
        if config.get("skills", "market_search", default=True):
            from backend.skills.market_search import search_and_install
            installed_name = await search_and_install(
                name,
                float(config.get("skills", "market_sim_threshold",
                                 default=0.45)))
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

    try:
        from backend.skills.forge import skill_forge

        # Try to discover and create this skill
        forge_result = await skill_forge.forge(
            task_description=f"Implement a function called {name} with params {json.dumps(params)}",
            provider=provider,
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

        return {
            "success": False,
            "error": f"Unknown skill '{name}' and forge failed: {forge_result.detail}",
            "forged": False,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Unknown skill '{name}': {e}",
            "forged": False,
        }


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _learn_procedure(messages: list[dict], tool_results: list[dict]) -> None:
    """Keep the route a turn actually took, when it worked.

    Called on the way out of a tool-using turn. It is a side effect of a reply
    that has already been produced, so it must never raise and must never
    delay or alter the response.
    """
    try:
        if not tool_results:
            return
        if not any(tr.get("success") for tr in tool_results):
            return
        from backend.sop.learn import record_run
        names = [str(tr.get("tool") or "") for tr in tool_results]
        record_run(None, names, _last_user_text(messages), success=True)
    except Exception as e:
        log.debug("Procedure learning skipped: %s", e)


async def chat_with_tools(
    provider,
    messages: list[dict],
    system_prompt: str = "",
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    model: str | None = None,
    tools: list[str] | None = None,
    reply_directive: str = "",
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
    """
    provider_id = getattr(provider, "provider_id", "unknown")
    uses_native = provider_id in NATIVE_TOOL_PROVIDERS
    # None means "no filter"; an empty list means "no tools", which is a
    # distinction a caller passing a restricted set depends on.
    only = set(tools) if tools is not None else None

    # Build full message list
    full_messages = []
    if system_prompt:
        full_messages.append({"role": "system", "content": system_prompt})
    full_messages.extend(messages)

    async def _final_answer(tool_results):
        """One more LLM call forced to plain text, using gathered results."""
        full_messages.append({
            "role": "user",
            "content": ("Answer the user's question now, based on the "
                        "information gathered above. Do not call any more "
                        "tools — respond with plain text."),
        })
        try:
            if uses_native:
                final = await _call_native_tools(provider, full_messages, model,
                                                 only, reply_directive)
            else:
                final = await _call_prompt_tools(provider, full_messages, model,
                                                 only, reply_directive)
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
    # Every tool result from every round. The per-round list below is what the
    # failure and round-cap paths reason about; this accumulates so a normal
    # text reply still reports what was actually called.
    all_tool_results: list[dict] = []
    while rounds < max_tool_rounds:
        rounds += 1

        if uses_native:
            result = await _call_native_tools(provider, full_messages, model,
                                              only, reply_directive)
        else:
            result = await _call_prompt_tools(provider, full_messages, model,
                                              only, reply_directive)

        # No tool call — normal text response
        if not result.get("tool_calls"):
            _learn_procedure(messages, all_tool_results
                             or result.get("tool_results", []))
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
            })
            executed.append((tc, exec_result))

        # Echo back only the calls whose skill actually exists. A name the model
        # invented (and the forge could not create) must not appear in the
        # assistant message: the API rejects the entire follow-up request, which
        # would lose the answer instead of reporting an unknown tool.
        valid = [(tc, res) for tc, res in executed
                 if skill_registry.get(tc["name"])]
        valid_ids = {tc.get("id") for tc, _ in valid if tc.get("id")}
        raw_tool_calls = [raw for raw in (result.get("raw_tool_calls") or [])
                          if raw.get("id") in valid_ids]
        if raw_tool_calls:
            # Keep the assistant tool_call message in history
            # (required by OpenAI-compatible APIs for the follow-up request)
            assistant_message: dict = {
                "role": "assistant",
                "content": result.get("response") or None,
                "tool_calls": raw_tool_calls,
            }
            # Thinking-mode models (DeepSeek v4) reject the follow-up request
            # unless the reasoning_content they returned is passed back. Only
            # set it when the provider actually sent it, so providers that
            # never do are not handed an unknown field.
            reasoning = result.get("reasoning_content")
            if reasoning:
                assistant_message["reasoning_content"] = reasoning
            full_messages.append(assistant_message)
        for tc, exec_result in valid:
            # Add to message history so provider sees the result
            full_messages.append(
                _tool_result_message(tc["name"], exec_result, tc.get("id")))
        all_tool_results.extend(tool_results)

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
        _learn_procedure(messages, all_tool_results or tool_results)
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


async def _call_native_tools(provider, messages: list[dict],
                             model: str | None = None,
                             only: set[str] | None = None,
                             reply_directive: str = "") -> dict:
    """Use native function-calling API (OpenAI/DeepSeek/Gemini)."""
    tools = skill_registry.to_openai_tools(only)
    messages = _with_directive(messages, reply_directive)

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

        if not result.ok:
            # Log it. Returning the message only puts it in the chat: a user
            # reporting "it errors" leaves nothing behind to diagnose, which is
            # exactly what happened with an OpenRouter 404.
            log.warning("Provider '%s' failed: %s",
                        getattr(provider, "provider_id", "?"), result.error)
            return {"response": f"[Provider error: {result.error}]", "tokens": 0}

        response_text = result.response or ""
        tokens = result.tokens_in + result.tokens_out

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
                             reply_directive: str = "") -> dict:
    """For providers without native tool support: inject tools into prompt."""
    tools_text = skill_registry.to_prompt_tools(only)

    # Inject tools into the last user message or system message
    modified_messages = list(messages)
    if modified_messages and modified_messages[-1]["role"] == "user":
        modified_messages[-1] = {
            "role": "user",
            "content": modified_messages[-1]["content"] + tools_text,
        }
    # After the catalogue, not before it: the catalogue is the bulk of what the
    # model reads before answering, so a language rule placed earlier is what it
    # overrides (see backend/language.py).
    modified_messages = _with_directive(modified_messages, reply_directive)

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

        response_text = result.response
        tokens = result.tokens_in + result.tokens_out

        parsed = _parse_tool_response(response_text)
        tool_calls = parsed["calls"]

        # Only strip the blocks that were actually tool calls, so any code the
        # model legitimately showed the user survives.
        visible_response = response_text
        for block in parsed["blocks"]:
            visible_response = visible_response.replace(block, "")
        visible_response = visible_response.strip()

        return {
            "response": visible_response or response_text,
            "tokens": tokens,
            "tool_calls": tool_calls,
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


def _parse_tool_response(text: str) -> dict:
    """Find tool calls, and the text blocks that should not be shown.

    Accepts ```tool, ```json or an unfenced object, on one line or several, so
    a small model is not required to match one exact format. Returns
    ``{"calls": [...], "blocks": [raw block, ...]}``.
    """
    if not text:
        return {"calls": [], "blocks": []}

    calls: list[dict] = []
    blocks: list[str] = []

    for match in re.finditer(r"```(?:tool|json)?\s*(.*?)```", text, re.DOTALL):
        inner = match.group(1).strip()
        found = False
        for candidate in _json_objects(inner):
            try:
                call = _as_tool_call(json.loads(candidate))
            except json.JSONDecodeError:
                continue
            if call:
                calls.append(call)
                found = True
        if found:
            blocks.append(match.group(0))

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

    return {"calls": calls, "blocks": blocks}


def _tool_result_message(tool_name: str, result: dict, tool_call_id: str | None = None) -> dict:
    """Format a tool/forge result as a message for the provider."""
    if result.get("success"):
        summary = json.dumps(result.get("data", {}), indent=2)[:3000]
        forged_note = " (newly forged!)" if result.get("forged") else ""
        content = f"Tool '{tool_name}' succeeded{forged_note}.\nResult:\n{summary}"
    else:
        content = f"Tool '{tool_name}' failed.\nError: {result.get('error', 'unknown')}"
    msg: dict = {"role": "tool", "content": content}
    if tool_call_id:
        msg["tool_call_id"] = tool_call_id
    return msg
