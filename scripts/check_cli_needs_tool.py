"""A missing capability ends by offering to build one, not by failing.

What this closes: when the model called a skill that did not exist and nothing
could be found or forged, the result was a bare error string. The user saw "I
couldn't do that" and had no way to know that building a tool was an option —
the feature existed, but nothing ever told them.

Two behaviours, and each is asserted here:

1. **The dead end is an offer.** The result carries `needs_tool` with the
   capability and a suggested name, and `requires_answer` — the mechanism the
   chat page and the character bubble already know how to render — so the UI can
   deep-link into Settings with the field pre-filled.
2. **The question is asked before anything is downloaded.** With
   `ask_before_build` on, an unknown capability raises "shall I build it, or
   search for one?" and ENDS the turn, exactly as the market and forge consent
   questions do. Declining skips to the search rather than leaving the request
   unmet.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_cli_needs_tool.py
"""

import asyncio
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    import backend.skills.tool_loop as tool_loop
    from backend.config import config

    # Nothing should be downloadable in this check: the point is the end of the
    # chain, so both acquisition paths are stubbed to "found nothing".
    import backend.skills.market_search as market_search

    async def _no_match(name, threshold=None):
        return None

    original_find = getattr(market_search, "find_match", None)
    market_search.find_match = _no_match

    import backend.skills.forge as forge_mod

    async def _no_forge(task_description, provider=None, auto_validate=True):
        class _R:
            success = False
            detail = "nothing could be written"
            skill_name = ""
        return _R()

    original_forge = getattr(forge_mod.skill_forge, "forge", None)
    forge_mod.skill_forge.forge = _no_forge

    try:
        print("=== the end of the chain is an offer, not an error ===")
        # Two things have to be true to reach the END of the chain rather than
        # one of the consent gates: the build offer is off (or it would raise its
        # own question and end the turn), and the forge has already been declined
        # or consented to (or it would raise its own). This is the state a turn
        # is genuinely in after the user has answered "no" to both.
        config.set("cli_tools", "ask_before_build", value=False)
        original_consented = tool_loop._consented
        tool_loop._consented = lambda *a, **k: True
        try:
            config.set("_forge", "request", value="")
        except Exception:  # noqa: BLE001
            pass
        try:
            result = asyncio.run(tool_loop.execute_skill(
                "convert_heic_to_png", {"path": "x.heic"}))
        finally:
            tool_loop._consented = original_consented
        check("the call reports failure", result.get("success") is False, result)
        data = result.get("data") or {}
        check("it carries a needs_tool payload", "needs_tool" in data, data)
        needs = data.get("needs_tool") or {}
        check("the payload names the capability",
              bool(needs.get("capability")), needs)
        check("the payload suggests a usable slug",
              bool(needs.get("suggested_slug"))
              and all(c.isalnum() or c == "-" for c in needs["suggested_slug"]),
              needs)
        check("the payload records the name that was called",
              needs.get("called_name") == "convert_heic_to_png", needs)
        check("it is marked as needing an answer",
              data.get("requires_answer") is True, data)
        check("the error points at where to build one",
              "CLI Tools" in str(result.get("error")),
              result.get("error"))
        check("the error does not claim the task is impossible",
              "impossible" not in str(result.get("error")).lower(),
              result.get("error"))

        print("=== the capability is described in the user's own words ===")
        config.set("_forge", "request",
                   value="turn my phone photos into png files")
        tool_loop._consented = lambda *a, **k: True
        try:
            result = asyncio.run(tool_loop.execute_skill(
                "convert_heic_to_png", {"path": "x.heic"}))
        finally:
            tool_loop._consented = original_consented
            config.set("_forge", "request", value="")
        needs = (result.get("data") or {}).get("needs_tool") or {}
        check("the brief uses the request text, not the function name",
              "phone photos" in str(needs.get("capability")), needs)

        print("=== the build question is asked before anything is downloaded ===")
        asked: list[str] = []

        async def _spy_ask(*args, **kwargs):
            asked.append(args[0] if args else "")
            return {"question_id": "q_test_1", "success": True}

        original_ask = tool_loop._ask_to_acquire
        tool_loop._ask_to_acquire = _spy_ask
        # A clean consent record, so the question is not suppressed as "already
        # asked" by an earlier block in this same run.
        try:
            config.set("_forge", "consent", value="")
        except Exception:  # noqa: BLE001
            pass
        config.set("cli_tools", "ask_before_build", value=True)
        try:
            result = asyncio.run(tool_loop.execute_skill(
                "convert_heic_to_png", {"path": "x.heic"}))
        finally:
            tool_loop._ask_to_acquire = original_ask

        check("the question was raised", bool(asked), asked)
        check("it was the build question, not the market one",
              any("build" in str(a).lower() for a in asked), asked)
        check("the turn was ended with requires_answer",
              (result.get("data") or {}).get("requires_answer") is True,
              result)
        check("the question id is carried back for the UI",
              bool((result.get("data") or {}).get("question_id")), result)

        print("=== the offer can be switched off ===")
        asked.clear()
        original_ask2 = tool_loop._ask_to_acquire
        tool_loop._ask_to_acquire = _spy_ask
        config.set("cli_tools", "ask_before_build", value=False)
        try:
            result = asyncio.run(tool_loop.execute_skill(
                "convert_heic_to_png", {"path": "x.heic"}))
        finally:
            tool_loop._ask_to_acquire = original_ask2
            config.set("cli_tools", "ask_before_build", value=True)
        # With the offer off, the forge's OWN consent question is the gate —
        # that is the pre-existing behaviour restored, and the spy records it.
        check("the build question is not raised when the offer is off",
              not any("build" in str(a).lower() for a in asked), asked)
        check("the forge's own question takes over instead",
              any("forge" in str(a).lower() or "write" in str(a).lower()
                  for a in asked), asked)

        print("=== with the feature off the old error shape returns ===")
        # Both questions off, so the chain runs to its end and the shape of the
        # result is what the pre-existing code produced: a plain failure with no
        # offer attached.
        asked.clear()
        config.set("cli_tools", "enabled", value=False)
        try:
            result = asyncio.run(tool_loop.execute_skill(
                "convert_heic_to_png", {"path": "x.heic"}))
        finally:
            config.set("cli_tools", "enabled", value=True)
            config.set("cli_tools", "ask_before_build", value=False)
        try:
            result = asyncio.run(tool_loop.execute_skill(
                "convert_heic_to_png", {"path": "x.heic"}))
        finally:
            config.set("cli_tools", "ask_before_build", value=True)
        check("nothing offers to build when cli_tools is off",
              "needs_tool" not in (result.get("data") or {}), result)
        check("but it still reports a failure rather than raising",
              result.get("success") is False, result)
    finally:
        if original_find is not None:
            market_search.find_match = original_find
        if original_forge is not None:
            forge_mod.skill_forge.forge = original_forge
        try:
            config.set("_forge", "request", value="")
        except Exception:  # noqa: BLE001
            pass

    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        for failure in fails:
            print(f"  - {failure}")
        return 1
    print("PASS: a missing capability ends by offering to build one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
