"""Addled assumed the live provider held 32768 tokens. Its gateway says 200000.

## What was measured

`providers.active` in the live settings is `9router`, whose `default_model` is
`Voxagent`. `budget.CONTEXT_LIMITS` has no `9router` entry, so
`context_limit("9router")` returned `FALLBACK_CONTEXT` -- which was 32768, the
smallest *local* window in the table.

The gateway publishes its own answer on the endpoint Addled already calls:

    GET http://localhost:20128/v1/models
      {'id': 'Voxagent',       'context_length': 200000,    'max_completion_tokens': 64000}
      {'id': 'cx/gpt-6.1-sol', 'context_length': 1050000,   'max_completion_tokens': 128000}
      {'id': 'cx/gpt-6-astra', 'context_length': 272000,    'max_completion_tokens': 128000}

A 6x under-estimate, and worse on other models on the same gateway. The effect
is silent: `fit_messages` trims older turns to fit a window that does not
exist, so long conversations lose history for no reason.

## Why the numbers were available and unused

`model_catalog._fetch_openai_compatible` already downloads exactly that payload
and kept only the `id` strings:

    mid = item.get("id") if isinstance(item, dict) else item
    if mid:
        models.append(str(mid))          # context_length was right there

## The fix

  * capture the window when fetching /models, under the field names seen in the
    wild (context_length, context_window/contextWindow, max_input_tokens,
    OpenRouter's nested top_provider.context_length);
  * consult it from `budget.context_limit()`, which now takes the model too,
    because one gateway serves 200k and 1.05M and a single per-provider number
    cannot describe both;
  * allow a hand-set `providers.<id>.context_limit` to win;
  * and when a provider published nothing, assume a cloud-plausible 128000 and
    say so at info -- 32768 remains the fallback only for the local runtimes
    that actually have a small, fixed window.

The ordering matters and is asserted here: this has to land before any token
budget for compaction, or the compactor summarises history the gateway would
have carried happily.

## What is asserted here

The extraction helper against the field names in the wild, `context_limit`'s
priority order, the fallback split between cloud and local, the catalog
round-trip, and that discovery keeps the numbers it used to discard.
"""

import asyncio
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

fails: list[str] = []


def check(label: str, ok: object, detail: object = "") -> None:
    if ok:
        print(f"  ok    {label}")
    else:
        fails.append(label)
        print(f"  FAIL  {label}")
        if detail:
            print(f"        {detail}")


def src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def extraction() -> None:
    """The window field is read under every spelling seen in the wild."""
    from backend.providers.model_catalog import _window_from_item

    check("the gateway's own field is read",
          _window_from_item({"id": "Voxagent", "context_length": 200000})
          == 200000)
    check("the OpenAI-docs spelling is read",
          _window_from_item({"id": "m", "context_window": 128000}) == 128000)
    check("the camelCase spelling is read",
          _window_from_item({"id": "m", "contextWindow": 272000}) == 272000)
    check("OpenRouter's nested field is read",
          _window_from_item({"id": "m",
                             "top_provider": {"context_length": 1050000}})
          == 1050000)
    check("a nested capabilities block is read",
          _window_from_item({"id": "m",
                             "capabilities": {"contextWindow": 872000}})
          == 872000)
    check("an entry that publishes nothing yields 0",
          _window_from_item({"id": "m"}) == 0)
    check("a junk value yields 0 rather than raising",
          _window_from_item({"id": "m", "context_length": "big"}) == 0)
    check("a zero or negative window is ignored",
          _window_from_item({"id": "m", "context_length": 0}) == 0
          and _window_from_item({"id": "m", "context_length": -1}) == 0)


def priority_order() -> None:
    """Published value beats the table; the table beats the fallback."""
    from backend.providers import budget, model_catalog
    from backend.config import config

    saved_limits = model_catalog.cached_limits

    try:
        # A provider the table knows: the table still answers.
        model_catalog.cached_limits = lambda pid: {}
        check("a table provider keeps its table window",
              budget.context_limit("deepseek") == 65536,
              budget.context_limit("deepseek"))
        check("a local runtime keeps its small window",
              budget.context_limit("ollama") == 32768
              and budget.context_limit("lmstudio") == 32768)

        # The live case: unlisted provider, published window.
        model_catalog.cached_limits = lambda pid: (
            {"Voxagent": 200000} if pid == "9router" else {})
        check("a published window overrides the fallback",
              budget.context_limit("9router", "Voxagent") == 200000,
              f"{budget.context_limit('9router', 'Voxagent')} - this exact "
              f"number was 32768 before the fix")
        check("the fallback is no longer the smallest local window",
              budget.context_limit("some-unknown-proxy") == 128000,
              f"{budget.context_limit('some-unknown-proxy')}")
        check("an unknown local-shaped provider stays small",
              budget.context_limit("nosuchlocal") == 128000,
              "only the known local runtimes are small")

        # Per-model: one gateway, two windows.
        model_catalog.cached_limits = lambda pid: (
            {"Voxagent": 200000, "cx/gpt-6.1-sol": 1050000}
            if pid == "9router" else {})
        check("the model is consulted, not just the provider",
              budget.context_limit("9router", "cx/gpt-6.1-sol") == 1050000,
              budget.context_limit("9router", "cx/gpt-6.1-sol"))
        check("one gateway can serve two different windows",
              budget.context_limit("9router", "Voxagent") == 200000)

        # A named model that is not in the map must not borrow another's number.
        model_catalog.cached_limits = lambda pid: (
            {"Voxagent": 200000} if pid == "9router" else {})
        check("an unknown model does not borrow a sibling's window",
              budget.context_limit("9router", "cx/unknown") == 128000,
              budget.context_limit("9router", "cx/unknown"))

        # A single published entry and no model named is unambiguous.
        model_catalog.cached_limits = lambda pid: (
            {"only": 65536} if pid == "solo" else {})
        check("a single published entry answers without a model name",
              budget.context_limit("solo") == 65536)

        # Several entries and no model named: the safe read is the smallest.
        model_catalog.cached_limits = lambda pid: (
            {"a": 200000, "b": 1050000} if pid == "multi" else {})
        check("no model named and several entries takes the smallest",
              budget.context_limit("multi") == 200000)

        # The trap this guard exists for: the live gateway lists a legacy
        # gpt-3.5-turbo at 16385 beside Voxagent at 200000. Taking the minimum
        # would size every unnamed request for the cheapest model on the
        # gateway — worse than the 32768 it replaced.
        model_catalog.cached_limits = lambda pid: (
            {"Voxagent": 200000, "gh/gpt-3.5-turbo": 16385}
            if pid == "gateway" else {})
        original_pc = config.provider_config
        config.provider_config = lambda pid: (
            {"default_model": "Voxagent"} if pid == "gateway" else {})
        try:
            check("the provider's own default model is preferred",
                  budget.context_limit("gateway") == 200000,
                  f"{budget.context_limit('gateway')} - min() over the "
                  f"gateway's models is 16385, which is a legacy entry")
        finally:
            config.provider_config = original_pc

        # With no default declared, several entries still take the smallest.
        model_catalog.cached_limits = lambda pid: (
            {"a": 200000, "b": 1050000} if pid == "multi" else {})
        check("without a declared default the smallest is still used",
              budget.context_limit("multi") == 200000,
              budget.context_limit("multi"))
    finally:
        model_catalog.cached_limits = saved_limits


def the_override() -> None:
    """A user-set providers.<id>.context_limit wins over everything."""
    from backend.providers import budget
    from backend.config import config
    from backend.providers import model_catalog

    original = config.provider_config

    try:
        config.provider_config = lambda pid: (
            {"context_limit": 8192} if pid == "pinned" else {})
        check("a hand-set override is honoured",
              budget.context_limit("pinned") == 8192,
              budget.context_limit("pinned"))
        check("the override beats a published window",
              budget.context_limit("deepseek") == 65536,
              "deepseek has no override, so the table still answers")
    finally:
        config.provider_config = original
        del model_catalog


def the_fallback_is_audible() -> None:
    """The old fallback was silent, which is how it survived."""
    body = src("backend/providers/budget.py")

    check("the fallback is logged at info, not debug",
          re.search(r'log\.info\(\s*"assuming a %d-token context', body)
          is not None,
          "the wrong number has to be visible in the log")
    check("the log names the provider and the override key",
          "providers.%s.context_limit to override" in body)
    check("the fallback is no longer the local 32768",
          "FALLBACK_CONTEXT = 128000" in body)
    check("local runtimes keep their own small fallback",
          "LOCAL_FALLBACK_CONTEXT = 32768" in body)


def _fetch_result_is_wired(model_catalog) -> bool:
    """Drive the real fetch against a stub response and read the result.

    Asserting on source text would pass on a fetch that parses the window and
    then drops it again; this checks the value that actually comes back.
    """


    payload = {"data": [
        {"id": "Voxagent", "context_length": 200000},
        {"id": "cx/gpt-6.1-sol", "context_length": 1050000},
        {"id": "bare"},
    ]}

    class _Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp()

    class _Httpx:
        AsyncClient = _Client

    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = _Httpx
    try:
        out = asyncio.run(model_catalog._fetch_openai_compatible(
            "stub", {"base_url": "http://127.0.0.1:1/v1"}))
    except Exception:
        return False
    finally:
        if saved is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = saved
    return (sorted(out.get("models") or []) ==
            ["Voxagent", "bare", "cx/gpt-6.1-sol"]
            and out.get("limits") == {"Voxagent": 200000,
                                      "cx/gpt-6.1-sol": 1050000})


def the_catalog_keeps_it() -> None:
    """Discovery stores the numbers instead of discarding them."""
    from backend.providers import model_catalog

    body = src("backend/providers/model_catalog.py")
    check("the fetch returns a limits map",
          '"limits": limits' in body)
    check("the window is extracted from each entry",
          "limits[mid] = window" in body,
          "the old code kept only the id string")
    check("a payload with windows yields both ids and limits",
          _fetch_result_is_wired(model_catalog),
          "the fetch must return the two maps together")
    check("cached_limits() reads the map back",
          "def cached_limits(" in body)
    check("the cache round-trips a limits map",
          model_catalog.cached_limits("__nope__") == {},
          "an unknown provider reads as empty rather than raising")
    check("a failed refresh keeps the previous limits",
          'entry.setdefault("limits", {})' in body,
          "same reasoning as the model list: a lookup failure is not a "
          "retraction")


def the_caller_passes_the_model() -> None:
    """The trim path asks about the model, not only the provider."""
    body = src("backend/skills/tool_loop.py")

    check("the trim site passes the resolved model",
          re.search(r"limit = budget\.context_limit\(\s*\n?\s*provider_id, "
                    r"str\(getattr\(provider, \"model\"", body) is not None,
          "one gateway serves 200k and 1.05M; the provider alone is not enough")
    check("trimming still uses that limit",
          "prompt_tokens + budget.MIN_REPLY_TOKENS > limit" in body)


def main() -> int:
    print("context window: the field the gateway publishes")
    extraction()
    print()
    print("which number wins:")
    priority_order()
    print()
    print("a hand-set override:")
    the_override()
    print()
    print("the fallback says what it assumed:")
    the_fallback_is_audible()
    print()
    print("discovery keeps the value instead of discarding it:")
    the_catalog_keeps_it()
    print()
    print("the caller asks about the model:")
    the_caller_passes_the_model()
    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        return 1
    print("PASS: the context window comes from the provider when it publishes "
          "one, the model is consulted, and an unknown window is assumed "
          "cloud-sized and said out loud")
    return 0


if __name__ == "__main__":
    sys.exit(main())
