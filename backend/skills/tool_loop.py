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

from backend.skills.registry import skill_registry, SkillResult

log = logging.getLogger("addled.tool_loop")

NATIVE_TOOL_PROVIDERS = {"openai", "deepseek", "gemini"}
MAX_TOOL_ROUNDS = 8


async def execute_skill(name: str, params: dict, provider=None) -> dict:
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

    # Skill not found — try forging it
    log.info("Skill '%s' not found — attempting forge", name)
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


async def chat_with_tools(
    provider,
    messages: list[dict],
    system_prompt: str = "",
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
) -> dict:
    """
    Run a chat completion with automatic tool execution.

    Flow:
      1. Send messages + tools to provider
      2. If provider returns a tool_call → execute skill → append result → repeat
      3. If provider returns text → done, return response
    """
    provider_id = getattr(provider, "provider_id", "unknown")
    uses_native = provider_id in NATIVE_TOOL_PROVIDERS

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
                final = await _call_native_tools(provider, full_messages)
            else:
                final = await _call_prompt_tools(provider, full_messages)
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
    while rounds < max_tool_rounds:
        rounds += 1

        if uses_native:
            result = await _call_native_tools(provider, full_messages)
        else:
            result = await _call_prompt_tools(provider, full_messages)

        # No tool call — normal text response
        if not result.get("tool_calls"):
            return {
                "response": result.get("response", ""),
                "tokens": result.get("tokens", 0),
                "tool_rounds": rounds,
                "tool_results": result.get("tool_results", []),
            }

        # Execute tool calls (with auto-forge for missing skills)
        tool_results = []
        raw_tool_calls = result.get("raw_tool_calls")
        if raw_tool_calls:
            # Keep the assistant tool_call message in history
            # (required by OpenAI-compatible APIs for the follow-up request)
            full_messages.append({
                "role": "assistant",
                "content": result.get("response") or None,
                "tool_calls": raw_tool_calls,
            })
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
            # Add to message history so provider sees the result
            full_messages.append(
                _tool_result_message(tc["name"], exec_result, tc.get("id")))

        # If all tools failed, try a forced plain-text answer before giving up
        if all(not tr["success"] for tr in tool_results):
            final = await _final_answer(tool_results)
            if final is not None:
                return final
            return {
                "response": "I tried to use some tools but they didn't work.",
                "tokens": 0,
                "tool_rounds": rounds,
                "tool_results": tool_results,
            }

    # Round cap reached: force one final answer from the gathered results.
    final = await _final_answer(tool_results if tool_results else [])
    if final is not None:
        return final

    return {
        "response": "I've completed the requested actions.",
        "tokens": 0,
        "tool_rounds": rounds,
        "tool_results": tool_results if tool_results else [],
    }


async def _call_native_tools(provider, messages: list[dict]) -> dict:
    """Use native function-calling API (OpenAI/DeepSeek/Gemini)."""
    tools = skill_registry.to_openai_tools()

    try:
        try:
            result = await provider.chat(
                messages,
                max_tokens=4096,
                temperature=0.7,
                tools=tools,
            )
        except TypeError:
            # Provider doesn't accept a tools kwarg → prompt-injected tools
            return await _call_prompt_tools(provider, messages)

        if not result.ok:
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


async def _call_prompt_tools(provider, messages: list[dict]) -> dict:
    """For providers without native tool support: inject tools into prompt."""
    tools_text = skill_registry.to_prompt_tools()

    # Inject tools into the last user message or system message
    modified_messages = list(messages)
    if modified_messages and modified_messages[-1]["role"] == "user":
        modified_messages[-1] = {
            "role": "user",
            "content": modified_messages[-1]["content"] + tools_text,
        }

    try:
        result = await provider.chat(
            modified_messages,
            max_tokens=4096,
            temperature=0.7,
        )

        if not result.ok:
            return {"response": f"[Provider error: {result.error}]", "tokens": 0}

        response_text = result.response
        tokens = result.tokens_in + result.tokens_out

        # Parse ```tool blocks from the response
        tool_calls = _extract_tool_calls(response_text)

        # Remove tool blocks from visible response
        visible_response = re.sub(
            r'```tool\s*\n.*?\n```', '', response_text, flags=re.DOTALL
        ).strip()

        return {
            "response": visible_response or response_text,
            "tokens": tokens,
            "tool_calls": tool_calls,
        }

    except Exception as e:
        log.warning("Prompt-based tool call failed: %s", e)
        return {"response": f"Error: {e}", "tokens": 0}


def _extract_tool_calls(text: str) -> list[dict]:
    """Extract tool calls from response text (```tool blocks or JSON)."""
    tool_calls = []

    # Pattern 1: ```tool\n{...}\n```
    for match in re.finditer(r'```tool\s*\n(.*?)\n```', text, re.DOTALL):
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict) and "tool" in data:
                tool_calls.append({
                    "name": data["tool"],
                    "params": data.get("params", {}),
                })
        except json.JSONDecodeError:
            continue

    # Pattern 2: Raw JSON with "tool" key (some models output this)
    if not tool_calls:
        for match in re.finditer(r'\{[^}]*"tool"\s*:\s*"[^"]+"[^}]*\}', text):
            try:
                data = json.loads(match.group(0))
                if isinstance(data, dict) and "tool" in data:
                    tool_calls.append({
                        "name": data["tool"],
                        "params": data.get("params", {}),
                    })
            except json.JSONDecodeError:
                continue

    return tool_calls


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
