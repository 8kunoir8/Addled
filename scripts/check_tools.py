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
import re
import sys
from pathlib import Path

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

    # A call written the way the catalogue prints each tool: `name(arguments)`.
    # Four replies in a row were this shape and were delivered to the user as
    # the answer, because nothing parsed them and no tool round ever happened.
    python_call = '```tool\nlist_dir("D:/work")\n```'
    check("a Python-shaped call is parsed",
          names(python_call) == ["list_dir"], str(calls(python_call)))
    check("and its argument lands on the name the schema declares",
          calls(python_call)[0]["params"] == {"path": "D:/work"},
          str(calls(python_call)))
    positional = '```tool\nwrite_file("notes.txt", "hello")\n```'
    check("a second positional argument takes the next declared name",
          calls(positional)[0]["params"]
          == {"path": "notes.txt", "content": "hello"},
          str(calls(positional)))
    keyword = '```tool\nlist_dir(path="D:/work", pattern="*.py")\n```'
    check("keyword arguments are read too",
          calls(keyword)[0]["params"]
          == {"path": "D:/work", "pattern": "*.py"},
          str(calls(keyword)))
    check("a bare call with no fence works",
          names('list_dir("D:/work")') == ["list_dir"], "")
    check("and that block is stripped from what the user sees",
          bool(_parse_tool_response(python_call)["blocks"]), "")
    # A guess at call syntax must not invent a call to something that is not a
    # skill: an unknown name starts the market-and-forge path.
    check("a call to a name that is not a skill is not invented",
          calls('```tool\nhalo("apa kabar")\n```') == [], "")

    # A reply cut off at the token cap has no closing fence and half a JSON
    # object. It has to be reported as unreadable — it is the reply that claimed
    # the file had been created.
    truncated = ('Saya telah membuat filenya.\n```tool\n{"tool": "write_file", '
                 '"params": {"path": "a.txt", "content": "isi panj')
    parsed = _parse_tool_response(truncated)
    check("an unterminated call is reported, not answered",
          parsed["calls"] == [] and len(parsed["malformed"]) == 1,
          str(parsed)[:200])
    check("and a complete call behind a missing fence still runs",
          names('```tool\n{"tool": "list_dir", "params": {"path": "D:"}}')
          == ["list_dir"], "")

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
          budget.estimate_tokens("a" * 400) == 133,
          str(budget.estimate_tokens("a" * 400)))
    check("and the estimate errs high rather than low",
          budget.estimate_tokens("a" * 400) > 400 / 4,
          "an under-estimate means a request is not trimmed and is rejected")
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

    # An intermediate sequence of turns must preserve surviving middle turns, not discard them.
    seq = [{"role": "system", "content": "s"},
           {"role": "user", "content": "turn 1"},
           {"role": "assistant", "content": "turn 2"},
           {"role": "user", "content": "turn 3"},
           {"role": "assistant", "content": "turn 4"},
           {"role": "user", "content": "turn 5"}]
    fitted_seq, dropped_seq = budget.fit_messages(seq, 25, 5)
    check("middle turns are preserved when they fit",
          len(fitted_seq) > 2 and fitted_seq[0]["content"] == "s" and fitted_seq[-1]["content"] == "turn 5",
          f"len={len(fitted_seq)}: {[m['content'] for m in fitted_seq]}")

    from backend.skills.registry import skill_registry
    filtered_tools = skill_registry.filter_for_query("can you check the weather in tokyo", max_tools=6)
    check("filter_for_query limits tools", len(filtered_tools) <= 6, f"len={len(filtered_tools)}")
    check("filter_for_query includes weather or web_search",
          bool({"weather", "web_search"} & filtered_tools),
          str(filtered_tools))

    # A question about tools must surface the tools that find other tools,
    # or a local model is told "you have no way to reach that" while an MCP
    # server sits connected.
    tool_ask = skill_registry.filter_for_query(
        "can you use your mcp tools and check server trust", max_tools=6)
    check("a tool question surfaces the discovery tools",
          {"find_mcp_server", "forge_skill", "list_forged"} & tool_ask,
          str(sorted(tool_ask)))
    # The discovery tools are offered for EVERY query, not only a tool-shaped
    # one. This assertion used to say the opposite — "a plain question does not
    # spend slots on discovery" — and that gate was the reason the agent looked
    # passive: asked to do a task, it was never shown that it could look for a
    # capability. The old check pinned the bug in place.
    plain = skill_registry.filter_for_query("what is 2 plus 2", max_tools=16)
    check("a plain question still offers the discovery tools",
          {"find_mcp_server", "forge_skill", "list_forged"} & plain ==
          {"find_mcp_server", "forge_skill", "list_forged"},
          str(sorted(plain)))
    # They are cheap: the whole catalogue stays well inside the local window.
    check("and offering them does not crowd out the core utilities",
          {"read_file", "run_command"} <= plain, str(sorted(plain)))

    # A question about documentation must surface the wiki tools, or a local
    # model answers "I don't know" while the user's own wiki has the answer.
    doc_ask = skill_registry.filter_for_query(
        "check the documentation for database migration")
    check("a documentation question surfaces wiki_search",
          "wiki_search" in doc_ask, str(sorted(doc_ask)))

    # A question about procedures must surface the SOP tools.
    proc_ask = skill_registry.filter_for_query(
        "what is the standard procedure for deploying a service")
    check("a procedure question surfaces sop_lookup",
          "sop_lookup" in proc_ask, str(sorted(proc_ask)))

    # The default max_tools is 16: enough for wiki, SOP, a domain MCP tool and
    # the three discovery tools to coexist. Raised from 10 when the discovery
    # tools stopped being gated — reserving their slots at 10 pushed seven real
    # skills out of reach, which check_reachability caught.
    default_q = skill_registry.filter_for_query("read a file and search the web")
    check("default max_tools allows up to 16",
          len(default_q) <= 16, f"len={len(default_q)}")


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


# ---- names models write, and the forge --------------------------------------

def run_forge_tests():
    """Defects measured against a real model on this machine.

    With the local 4B model, two of three end-to-end tool runs answered
    "Unknown skill 'get_screen_size()'" — the name arrived with parentheses and
    the parser kept them. An unmatched name does not merely fail, it reads as a
    *missing* skill, so the market-and-forge path ran, its module template did
    not compile, and the file it wrote stayed on disk and failed to load on
    every later start. The forge's own pip command was wrong as well, so a
    dependency it had just decided it needed was never installed.
    """
    from backend.skills import forge
    from backend.skills.tool_loop import (_normalise_tool_name,
                                          _parse_tool_response)
    from backend.providers import budget

    # ---- 1. the name a model actually writes ----
    for written, expected in (
        ("get_screen_size()", "get_screen_size"),
        ("get_screen_size( )", "get_screen_size"),
        ("get_screen_size()  ", "get_screen_size"),
        ('"get_screen_size"', "get_screen_size"),
        ("`get_screen_size`", "get_screen_size"),
        ("get_screen_size.", "get_screen_size"),
        ("get_screen_size (no arguments)", "get_screen_size"),
        ("list_dir", "list_dir"),
        ("mcp__filesystem__read_file", "mcp__filesystem__read_file"),
    ):
        got = _normalise_tool_name(written)
        check(f"a tool written as {written!r} resolves to the skill",
              got == expected, f"got {got!r}")

    # The helper is only useful if it holds through the real parse.
    parsed = _parse_tool_response(
        '```tool\n{"tool": "get_screen_size()", "params": {}}\n```')
    check("a parenthesised call parses to the real name",
          [c["name"] for c in parsed["calls"]] == ["get_screen_size"],
          str(parsed["calls"]))
    # Positive control: the parameters must still come through, or the fix has
    # traded one wrong answer for another.
    parsed = _parse_tool_response(
        '```tool\n{"tool": "list_dir()", "params": {"path": "D:\\\\x"}}\n```')
    check("and a parenthesised call keeps its parameters",
          parsed["calls"] and parsed["calls"][0]["params"] == {"path": "D:\\x"},
          str(parsed["calls"]))

    # ---- 2. a generated module has to compile ----
    generated = ('async def probe_skill(params: dict) -> dict:\n'
                 '    """A generated skill."""\n'
                 '    try:\n'
                 '        return {"success": True, "echo": params.get("x")}\n'
                 '    except Exception as e:\n'
                 '        return {"success": False, "error": str(e)}\n')
    module = forge.build_skill_module(
        "probe_skill", "somepkg",
        'A description with a "quote" and\nnewline',
        generated, {"x": "string"})

    compiled = None
    try:
        compiled = compile(module, "probe_skill.py", "exec")
        check("a forged module compiles", True)
    except SyntaxError as exc:
        check("a forged module compiles", False,
              f"{exc.msg} at line {exc.lineno}")

    if compiled is not None:
        namespace: dict = {}
        try:
            exec(compiled, namespace)
            defined = namespace.get("SKILL_DEF")
            check("it defines SKILL_DEF", defined is not None, "no SKILL_DEF")
            if defined is not None:
                check("named after the skill",
                      getattr(defined, "name", None) == "probe_skill",
                      str(getattr(defined, "name", None)))
                check("and points a handler at the function",
                      getattr(defined, "handler", None) is not None, "")
                check("an awkward description survives",
                      '"quote"' in str(getattr(defined, "description", "")),
                      repr(getattr(defined, "description", ""))[:80])
                check("and it is a single line",
                      "\n" not in str(getattr(defined, "description", "")),
                      repr(getattr(defined, "description", ""))[:80])
                check("with the newline collapsed, not dropped",
                      "newline" in str(getattr(defined, "description", "")),
                      repr(getattr(defined, "description", ""))[:80])
        except Exception as exc:  # noqa: BLE001
            check("it loads", False, f"{type(exc).__name__}: {exc}")

    # ---- 4. the handler must bind to a function that exists ----
    # The skill name is a slug of the task text, so the model is asked for
    # "get_this_machine_hostname" and naturally writes "probe_machine_name"
    # instead. Binding SKILL_DEF.handler to the requested name then produced a
    # module that raised NameError at import, so every forged skill was written
    # and deleted again. Proven end to end by scripts/_forge_probe.py.
    generated_other_name = (
        'async def probe_machine_name(params: dict) -> dict:\n'
        '    return {"success": True}\n')
    module2 = forge.build_skill_module(
        "get_this_machine_s_hostname", "socket", "get the hostname",
        generated_other_name, {"type": "object", "properties": {}})

    check("the defined function is found by name",
          forge.defined_async_functions(generated_other_name)
          == ["probe_machine_name"],
          str(forge.defined_async_functions(generated_other_name)))

    ns: dict = {}
    try:
        exec(compile(module2, "m.py", "exec"), ns)
        bound = ns.get("SKILL_DEF")
        check("a module whose function is named differently still loads",
              bound is not None, "no SKILL_DEF")
        check("and its handler is the function that exists",
              getattr(bound, "handler", None)
              is ns.get("probe_machine_name"),
              str(getattr(bound, "handler", None)))
        check("while the skill keeps the requested name",
              getattr(bound, "name", None) == "get_this_machine_s_hostname",
              str(getattr(bound, "name", None)))
    except Exception as exc:  # noqa: BLE001
        check("a module whose function is named differently still loads",
              False, f"{type(exc).__name__}: {exc}")

    # The long-slug case: the prompt must stop demanding an unusable name.
    # The requested name is interpolated into the prompt, so a long slug would
    # appear literally in the source as `async def {skill_name}(`, guarded by
    # the length check at the call site.
    forge_source = Path(ROOT, "backend", "skills", "forge.py").read_text(
        encoding="utf-8")
    check("the name given to the model is guarded by a length check",
          "requested = skill_name if len(skill_name) <= 40 else" in forge_source,
          "no length guard around the requested function name")
    check("and an unnamed variant exists for the long case",
          "a_short_snake_case_name" in forge_source,
          "nothing tells the model what to do when the slug is unusable")

    # ---- 5. the pip command ----
    argv = forge.pip_argv("pip install numpy")
    check("pip install becomes python -m pip install, not python -m install",
          argv is not None and argv[1:3] == ["-m", "pip"]
          and argv[-1] == "numpy", str(argv))
    check("using the interpreter that is running",
          argv is not None and argv[0] == sys.executable, str(argv))
    check("pip3 is handled the same way",
          forge.pip_argv("pip3 install x")[1:3] == ["-m", "pip"],
          str(forge.pip_argv("pip3 install x")))
    check("an explicit python -m pip is passed through",
          forge.pip_argv("python -m pip install y")
          == ["python", "-m", "pip", "install", "y"],
          str(forge.pip_argv("python -m pip install y")))
    check("anything that is not a pip install is refused",
          forge.pip_argv("rm -rf /") is None, str(forge.pip_argv("rm -rf /")))
    check("an empty command is refused", forge.pip_argv("") is None, "")

    # ---- 4. the forge's own prompts must fit the window ----
    # The forge builds a one-message prompt and hands it straight to
    # provider.chat, which bypasses the tool loop and its trimming. On the 8k
    # local model the generation prompt plus a 2000-token request overran the
    # window and surfaced as an opaque provider error.
    big = "x" * 200000
    clipped, room = budget.fit_single_prompt(big, "local", want_reply=2000)
    check("an oversized background prompt is clipped",
          len(clipped) < len(big), f"{len(big)} -> {len(clipped)}")
    check("and the clipped prompt fits the local window",
          budget.estimate_tokens(clipped) + room + 64
          <= budget.context_limit("local"),
          f"tokens={budget.estimate_tokens(clipped)} room={room}")
    check("with real reply room left", room >= 256, str(room))
    small, room_small = budget.fit_single_prompt("hi", "local", want_reply=2000)
    check("a prompt that fits is untouched", small == "hi", small[:20])
    check("and keeps the reply it asked for", room_small == 2000, str(room_small))

    # ---- 5. forging must not be done by the weakest model available ----
    from backend.skills import tool_loop

    class _P:
        def __init__(self, pid):
            self.provider_id = pid

    def _fake_get_provider(pid=None):
        return _P(str(pid))

    import backend.providers.registry as reg
    import backend.providers.selector as sel
    saved_get, saved_resolve = reg.get_provider, sel.resolve_default_provider
    reg.get_provider = _fake_get_provider
    try:
        sel.resolve_default_provider = lambda: "deepseek"
        borrowed = tool_loop.forge_target_provider(_P("local"))
        check("a local chat provider hands the forge a cloud model",
              getattr(borrowed, "provider_id", "") == "deepseek",
              str(getattr(borrowed, "provider_id", "")))

        sel.resolve_default_provider = lambda: "local"
        kept = tool_loop.forge_target_provider(_P("local"))
        check("and nothing better on offer keeps the local model",
              getattr(kept, "provider_id", "") == "local",
              str(getattr(kept, "provider_id", "")))

        cloud = tool_loop.forge_target_provider(_P("deepseek"))
        check("a capable chat provider forges for itself",
              getattr(cloud, "provider_id", "") == "deepseek",
              str(getattr(cloud, "provider_id", "")))
    finally:
        reg.get_provider, sel.resolve_default_provider = saved_get, saved_resolve

    # ---- 6. the auto-forge uses the user's request, not the tool name ----
    # `Implement a function called scrape_website` was handed to a web search as
    # "python library implement a function called scrape_website pip install",
    # which discovers nothing. The real request does.
    source = Path(ROOT, "backend", "skills", "tool_loop.py").read_text(
        encoding="utf-8")
    check("the auto-forge no longer forges from the bare tool name",
          "Implement a function called {name}" not in source,
          "the old placeholder description is still there")
    body = re.search(
        r"async def _execute_skill_inner\(.*?\n(?=\ndef |\nasync def )",
        source, re.S)
    check("it reads the published request instead",
          bool(body) and "_forge" in body.group(0), "no _forge lookup found")

    ws = Path(ROOT, "backend", "ws_server.py").read_text(encoding="utf-8")
    check("and the chat pipeline publishes that request",
          'config.set("_forge", "request"' in ws,
          "nothing writes _forge.request")


# ---- shell access: the agent must not deny having it ------------------------

def run_shell_access_tests():
    """The agent told the user it had no access to their system.

    Three things were behind that. The injected capability list never mentioned
    the shell, so asked about its abilities the model read a list that excluded
    it and answered honestly. `run_command` declared requires_approval and
    nothing consumed it. And the approval it *did* produce had no id and no
    answerer, because the skill called TerminalExecutor directly and skipped the
    action executor that owns the approval handle.
    """
    import asyncio
    from backend.actions.executor import ActionExecutor
    from backend.safety.destruction_gate import DestructionGate
    from backend.skills.registry import skill_registry
    from backend.tool_brief import capabilities_block

    # Asked of the prompt that is actually built, not of a string in a file.
    # This used to grep `ws_server.py` for `capabilities_ctx`, which tied the
    # test to where the text was stored: moving the block into `tool_brief`
    # (so a persona could no longer drop it) broke the check while the behaviour
    # it cares about was unchanged and in fact improved. Building the prompt is
    # the only version that cannot pass on a stale copy.
    prompt = capabilities_block(None)
    check("the capability list names the shell",
          "run_command" in prompt,
          "the prompt never mentions that commands can be run")
    check("and says the agent does have system access",
          "DO have access to the user's system" in prompt,
          "nothing contradicts the 'I cannot access your system' answer")

    # The block has to reach a persona turn too. A swarm agent passing a persona
    # used to lose it, and then answered "those tools aren't accessible here".
    ws = Path(ROOT, "backend", "ws_server.py").read_text(encoding="utf-8")
    check("the capability block is appended on the persona path as well",
          "capabilities_block(tools)" in ws,
          "a persona turn would be told nothing about its tools")

    skill = skill_registry.get("run_command")
    check("run_command is registered", skill is not None)
    check("and declares that it needs approval",
          bool(getattr(skill, "requires_approval", False)),
          str(getattr(skill, "requires_approval", None)))
    check("its description tells the model it has real access",
          "real access to the user's machine" in (skill.description or ""),
          (skill.description or "")[:120])

    src = Path(ROOT, "backend", "skills", "registry.py").read_text(
        encoding="utf-8")
    check("the skill goes through the executor, not the terminal directly",
          "execute_for_chat" in src,
          "run_command bypasses the approval owner again")

    ex = ActionExecutor(gate=DestructionGate())

    async def drive():
        # A safe command must run immediately and need no approval.
        safe = await ex.execute_for_chat("run_command",
                                         {"command": "Write-Output shell-ok"})
        safe_out = str((safe.data or {}).get("stdout") or "")
        check("a safe command runs without approval", safe.success,
              f"{safe.error} {safe_out[:60]}")
        check("and really reached the shell", "shell-ok" in safe_out,
              repr(safe_out[:80]))
        check("nothing was queued for it", not ex.pending_approvals(),
              str(ex.pending_approvals()))

        # A destructive one must queue with a usable handle, and must NOT hold
        # the turn open. It used to wait in-band for the user, which meant the
        # answer usually arrived after the caller had given up — the user saw a
        # timeout rather than the prompt.
        blocked = await ex.execute_for_chat(
            "run_command", {"command": "rm -rf /tmp/addled-probe"})
        pending = ex.pending_approvals()
        check("a destructive command does not block the turn", not blocked.success,
              "it ran, or it waited")
        check("and it is queued for the user to answer", bool(pending),
              "it was not queued")
        check("and the queue carries a real approval id",
              bool(pending)
              and str(pending[0].get("approval_id")).startswith("appr_"),
              str(pending[:1]))
        check("and the turn reports that approval is needed",
              bool((blocked.data or {}).get("requires_approval")),
              str(blocked.data)[:120])
        check("and it does not tell the user to ask again",
              "ask again" not in str(blocked.error or ""),
              str(blocked.error)[:120])

        # Approving runs it there and then, because nothing else will. Asserted
        # on the command having been DISPATCHED, not on it succeeding: this is a
        # PowerShell host, so `rm -rf` is expected to fail at the shell. What is
        # being checked is that approving reached the shell at all.
        approved = await ex.approve(pending[0]["approval_id"])
        reached = bool((approved.data or {}).get("exit_code") is not None
                       or (approved.data or {}).get("stdout")
                       or (approved.data or {}).get("stderr"))
        check("approving runs the deferred command",
              reached, f"{approved.success} {approved.error} {approved.data}")

        # Denial is a separate answer, and reports itself as one.
        denied = await ex.execute_for_chat(
            "run_command", {"command": "rm -rf /tmp/addled-probe-2"})
        aid2 = (denied.data or {}).get("approval_id")
        ex.deny(aid2)
        check("denying clears the request",
              aid2 not in {p.get("approval_id")
                           for p in ex.pending_approvals()},
              "it is still queued after being denied")

    asyncio.run(drive())

    # The retry nudge is an MCP mechanism; sending a shell command round again
    # repeats a call that cannot succeed, because run_command has no `confirm`.
    loop_src = Path(ROOT, "backend", "skills", "tool_loop.py").read_text(
        encoding="utf-8")
    check("the confirm retry is scoped to MCP tools only",
          'startswith("mcp__")' in loop_src,
          "the nudge also fires for shell commands")


def main():
    run_catalogue_tests()
    run_parser_tests()
    run_budget_tests()
    run_error_tests()
    run_bot_tests()
    run_forge_tests()
    run_shell_access_tests()
    print()
    print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


sys.exit(main())
