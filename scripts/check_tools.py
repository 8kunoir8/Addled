"""Tool-catalogue, parser and provider-error checks.

Three separate defects motivated these, all of them invisible until measured:

  * the prompt-based tool catalogue was ~4,700 tokens against a local context
    that had 4,096 left after its reply budget — so tool use there could not
    work at all;
  * ``_extract_tool_calls`` could not parse a call with parameters, because its
    fallback regex cannot cross a closing brace;
  * provider errors surfaced httpx's status line and discarded the response
    body, which is where OpenRouter explains *why* it refused.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_tools.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import httpx  # noqa: E402

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


# ---- the catalogue has to leave room for the conversation -------------------

def run_catalogue_tests():
    from backend.config import config
    from backend.providers import budget
    from backend.skills.registry import SkillRegistry

    config._ensure_loaded()
    registry = SkillRegistry()
    skills = registry.enabled_list_all()
    block = registry.to_prompt_tools()

    limit = budget.context_limit("local")
    room = limit - budget.MIN_REPLY_TOKENS
    tokens = budget.estimate_tokens(block)

    print(f"   tool block: {len(block):,} chars, ~{tokens:,} tokens "
          f"(measured 4,683 before this change, against {room:,} available)")
    check("the catalogue fits the local model's context",
          tokens < room, f"~{tokens} tokens vs {room} available")
    check("it leaves room for the conversation",
          tokens < room * 0.6, f"uses {tokens / room:.0%} of the window")

    # One line per tool, and every tool still listed.
    lines = [ln for ln in block.splitlines() if " — " in ln]
    check("every tool is still advertised", len(lines) == len(skills),
          f"{len(lines)} lines for {len(skills)} skills")

    sample = next(s for s in skills if s.name == "list_dir") \
        if any(s.name == "list_dir" for s in skills) else skills[0]
    desc = sample.to_prompt_desc()
    check("a description is a single line", "\n" not in desc, repr(desc[:60]))
    check("argument names survive", "(" in desc and ")" in desc, desc[:80])
    check("optional arguments are marked",
          "?" in desc or not sample.parameters.get("properties"), desc[:80])
    check("the description is clipped",
          len(desc) < 200, f"{len(desc)} chars")

    # The full schema must still reach native providers untouched.
    native = registry.to_openai_tools()
    check("native providers still get full schemas",
          all(isinstance(t.get("function", {}).get("parameters"), dict)
              for t in native), "")
    big = next(t for t in native if t["function"]["name"] == sample.name)
    check("the native schema is not clipped",
          isinstance(big["function"]["parameters"].get("properties"), dict), "")
    check("the native description is not clipped",
          len(big["function"]["description"]) >= len(sample.description.rstrip("…")),
          "")


# ---- the parser has to accept how small models actually answer ---------------

def run_parser_tests():
    from backend.skills.tool_loop import _extract_tool_calls, _parse_tool_response

    def calls(text):
        return _parse_tool_response(text)["calls"]

    def names(text):
        return [c["name"] for c in calls(text)]

    # The native path falls back to this and iterates the result as a list, so
    # a dict return silently turns tool calls into single characters.
    check("_extract_tool_calls still returns a list",
          isinstance(_extract_tool_calls('{"tool": "list_dir"}'), list),
          type(_extract_tool_calls('{"tool": "list_dir"}')).__name__)
    check("and that list holds the calls",
          [c["name"] for c in _extract_tool_calls('{"tool": "list_dir"}')]
          == ["list_dir"], "")

    # The documented shape.
    fenced = '```tool\n{"tool": "list_dir", "params": {"path": "D:\\\\x"}}\n```'
    check("a fenced call is parsed", names(fenced) == ["list_dir"], str(calls(fenced)))
    # A parameter worth having is the whole point — the old fallback could not
    # see past the inner brace.
    check("nested params survive",
          calls(fenced)[0]["params"] == {"path": "D:\\x"}, str(calls(fenced)))

    check("a single-line fence works",
          names('```tool {"tool": "read_file", "params": {"path": "a.txt"}}```')
          == ["read_file"], "")
    check("a ```json fence works",
          names('```json\n{"tool": "wiki_search", "params": {"query": "x"}}\n```')
          == ["wiki_search"], "")
    check("a bare object works",
          names('Sure! {"tool": "memory_files", "params": {}}')
          == ["memory_files"], "")
    check("a ```tool fence without params works",
          names('```tool\n{"tool": "screenshot"}\n```') == ["screenshot"], "")
    check("alternative keys are accepted",
          names('{"name": "list_dir", "parameters": {"path": "D:"}}')
          == ["list_dir"], "")
    check("a function-style call is accepted",
          names('{"function": {"name": "read_file", '
                '"arguments": "{\\"path\\": \\"a.txt\\"}"}}') == ["read_file"], "")

    # Braces inside strings must not break the scan.
    tricky = ('```tool\n{"tool": "run_command", "params": '
              '{"command": "echo {\\"a\\": 1}"}}\n```')
    check("braces inside a string do not confuse it",
          names(tricky) == ["run_command"], str(calls(tricky)))
    check("the command's braces are intact",
          calls(tricky)[0]["params"]["command"] == 'echo {"a": 1}',
          str(calls(tricky)))

    # Nothing to find.
    check("prose yields nothing", calls("I cannot do that.") == [], "")
    check("empty input yields nothing", calls("") == [], "")
    check("a json object that is not a call yields nothing",
          calls('```json\n{"answer": 42}\n```') == [], "")

    # Only real tool blocks are hidden from the user.
    parsed = _parse_tool_response(
        'Let me look.\n```tool\n{"tool": "list_dir"}\n```\nDone.')
    check("the tool block is reported for stripping",
          len(parsed["blocks"]) == 1, str(parsed["blocks"]))
    parsed = _parse_tool_response('```python\nprint(1)\n```')
    check("ordinary code blocks are not stripped",
          parsed["blocks"] == [] and parsed["calls"] == [], str(parsed))


# ---- budgeting ---------------------------------------------------------------

def run_budget_tests():
    from backend.providers import budget

    check("text is estimated, not zero",
          budget.estimate_tokens("a" * 400) == 100,
          str(budget.estimate_tokens("a" * 400)))
    check("empty text is zero", budget.estimate_tokens("") == 0)
    check("non-string content is handled",
          budget.message_tokens([{"role": "user", "content": [{"a": 1}]}]) > 0)

    check("local reports its configured window",
          budget.context_limit("local") == 8192,
          str(budget.context_limit("local")))
    check("an unknown provider gets a usable default",
          budget.context_limit("nope") == budget.FALLBACK_CONTEXT, "")

    messages = [{"role": "system", "content": "sys"}]
    messages += [{"role": "user", "content": "x" * 2000} for _ in range(10)]
    fitted, dropped = budget.fit_messages(messages, 4096, 1024)
    check("history is trimmed to fit", dropped > 0, str(dropped))
    check("the system prompt survives",
          fitted[0]["role"] == "system", str(fitted[0]))
    check("the newest turn survives",
          fitted[-1] is messages[-1], "the last message was dropped")
    check("the result fits",
          budget.message_tokens(fitted) <= 4096 - 1024,
          str(budget.message_tokens(fitted)))

    # A single huge turn cannot be trimmed away — the caller must be told.
    huge = [{"role": "system", "content": "s"},
            {"role": "user", "content": "x" * 40000}]
    fitted_huge, dropped_huge = budget.fit_messages(huge, 4096, 1024)
    check("an untrimmable request is still handed back smaller or equal",
          budget.message_tokens(fitted_huge) <= budget.message_tokens(huge),
          str(budget.message_tokens(fitted_huge)))
    check("and its reply budget collapses to zero",
          budget.reply_budget(budget.message_tokens(fitted_huge), 4096) == 0,
          str(budget.reply_budget(budget.message_tokens(fitted_huge), 4096)))

    check("a request that fits is untouched",
          budget.fit_messages([{"role": "user", "content": "hi"}], 8192, 1024)
          == ([{"role": "user", "content": "hi"}], 0))
    check("nothing is dropped when only the system prompt and recent turns exist",
          budget.fit_messages([{"role": "system", "content": "s"},
                               {"role": "user", "content": "u"}],
                              100, 50)[1] == 0)

    check("the reply budget shrinks with a large prompt",
          budget.reply_budget(7000, 8192, want=4096) < 4096,
          str(budget.reply_budget(7000, 8192, want=4096)))
    check("the reply budget is capped by what was wanted",
          budget.reply_budget(100, 8192, want=4096) == 4096, "")
    check("a prompt that cannot fit reports zero",
          budget.reply_budget(9000, 8192, want=4096) == 0,
          str(budget.reply_budget(9000, 8192, want=4096)))


# ---- provider errors must explain themselves ---------------------------------

def run_error_tests():
    from backend.providers.openai_provider import http_error_detail

    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    policy = httpx.Response(
        404, request=request,
        json={"error": {"message":
                        "0 endpoints out of 1 requested are available matching "
                        "your guardrail restrictions and data policy.\n"
                        "configurable at https://openrouter.ai/settings/privacy",
                        "code": 404}})
    detail = http_error_detail(httpx.HTTPStatusError(
        "Client error '404 Not Found'", request=request, response=policy))
    print(f"   surfaced: {detail[:100]}…")
    check("the status is kept", detail.startswith("HTTP 404:"), detail[:40])
    check("the upstream message is included",
          "guardrail restrictions" in detail, detail[:120])
    check("the fix is not lost", "settings/privacy" in detail, detail[:160])
    check("the newlines are flattened", "\n" not in detail, repr(detail[:80]))

    plain = httpx.Response(500, request=request, text="boom")
    detail = http_error_detail(httpx.HTTPStatusError(
        "Server error '500'", request=request, response=plain))
    check("a non-JSON body still surfaces", "boom" in detail, detail)

    empty = httpx.Response(503, request=request, text="")
    detail = http_error_detail(httpx.HTTPStatusError(
        "Server error '503'", request=request, response=empty))
    check("an empty body falls back to the status",
          "503" in detail, detail)


def run_bot_tests():
    """The bot bridges must be able to load.

    All three required '../shared/ws-client' from inside bots/, which resolves
    to <root>/shared and does not exist — so every bot died on require and the
    dashboard page could never have worked.
    """
    from pathlib import Path

    root = Path(ROOT) / "bots"
    shared = root / "shared" / "ws-client.js"
    check("bots/shared/ws-client.js exists", shared.is_file(), str(shared))

    for name in ("telegram-bot.js", "discord-bot.js", "whatsapp-bot.js"):
        script = root / name
        check(f"{name} exists", script.is_file(), str(script))
        if not script.is_file():
            continue
        text = script.read_text(encoding="utf-8")
        check(f"{name} requires ./shared/ws-client",
              "require('./shared/ws-client')" in text,
              "still using a path that resolves outside bots/")
        check(f"{name} does not use ../shared",
              "require('../shared/" not in text,
              "this path cannot resolve from inside bots/")

    from backend.bots import manager as bots
    check("the manager knows all three platforms",
          set(bots.PLATFORMS) == {"telegram", "discord", "whatsapp"},
          str(sorted(bots.PLATFORMS)))
    reported = bots.status()["platforms"]
    for pid, info in reported.items():
        check(f"{pid}: the script is locatable", info["script_found"],
              info["script_path"])
        check(f"{pid}: blockers are a list", isinstance(info["blockers"], list),
              str(info["blockers"]))


def main():
    run_catalogue_tests()
    run_parser_tests()
    run_budget_tests()
    run_error_tests()
    run_bot_tests()
    print()
    print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


sys.exit(main())
