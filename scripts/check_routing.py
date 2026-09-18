"""Router regression check: role classification + the no-op guarantee.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_routing.py

The important assertion is the second block: with no role overrides configured,
every provider must resolve to exactly the model Addled sent before routing
existed. That keeps an existing install's behaviour unchanged.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import config            # noqa: E402
from backend.providers import router         # noqa: E402

fails = []

# The shipped role defaults, restated here rather than read from the router so
# the test fails if one is changed by accident. Precedence at resolution time is
# explicit config -> these -> the provider's default_model.
SHIPPED = {
    "deepseek": {"chat": "deepseek-v4-flash",
                 "reasoning": "deepseek-v4-pro",
                 "utility": "deepseek-chat"},
}


def check(label, got, want):
    if got != want:
        fails.append(f"{label}: got {got!r}, want {want!r}")


class _Stub:
    """Minimal stand-in for a provider object."""

    def __init__(self, provider_id: str):
        self.provider_id = provider_id


# ---- 1. classification -------------------------------------------------------
CASES = [
    ("hi", "chat", {}),
    ("what is 17 times 3", "chat", {}),
    ("thanks!", "chat", {}),
    ("what is the capital of France?", "chat", {}),
    ("open the calculator", "chat", {}),
    ("explain why the sky is blue", "reasoning", {}),
    ("debug why the scheduler fires immediately", "reasoning", {}),
    ("refactor this function please", "reasoning", {}),
    ("compare postgres vs sqlite for this", "reasoning", {}),
    ("should I use redis or an in-memory cache", "reasoning", {}),
    ("prove that this loop terminates", "reasoning", {}),
    ("what is the root cause of this 500 error", "reasoning", {}),
    ("what are the trade-offs here", "reasoning", {}),
    ("plan out the migration steps", "reasoning", {}),
    ("summarize this document for me", "long", {}),
    ("read the whole file and fix it", "long", {}),
    ("x" * 1300, "long", {}),
    ("describe this", "vision", {"has_attachments": True, "image_only": True}),
    ("what is in the image", "vision",
     {"has_attachments": True, "image_only": True}),
    ("describe this", "chat", {"has_attachments": True, "image_only": False}),
    ("rename x to y", "chat", {"code_hint": True}),
    ("rename x to y in the file", "chat", {"code_hint": True}),
    ("please update this function to handle the new payload shape and "
     "make sure the error handling still works", "reasoning", {"code_hint": True}),
    ("here is my code\n```python\n" + "\n".join(
        f"x{i} = {i}" for i in range(6)) + "\n```", "reasoning",
     {"code_hint": True}),
]

for msg, want, kwargs in CASES:
    check(f"classify({msg[:40]!r},{kwargs})", router.classify(msg, **kwargs), want)

# explicit @role tags
check("tag reasoning", router.split_role_tag("@reasoning hello")[0], "reasoning")
check("tag strips", router.split_role_tag("@reasoning hello")[1], "hello")
check("tag chat", router.split_role_tag("@chat: explain why")[0], "chat")
check("no tag", router.split_role_tag("hello")[0], None)
check("tag beats heuristics",
      router.pick("deepseek", "@chat explain why this fails")[0], "chat")

# ---- 2. no-op guarantee ------------------------------------------------------
# Every provider, every role, must resolve to the value Addled would have sent
# before routing existed (default_model, or vision_model for the vision role).
builtin = config.get("providers", "builtin", default={})
saved_catalog = router._catalog_models
router._catalog_models = lambda pid: []      # ignore any live model catalog
for pid, pcfg in builtin.items():
    default_model = pcfg.get("default_model") or ""
    vision_model = pcfg.get("vision_model") or default_model
    roles = pcfg.get("roles") or {}
    for role in router.ROLES:
        want = ((roles.get(role) or "").strip()
                or (SHIPPED.get(pid) or {}).get(role)
                or (vision_model if role == "vision" else default_model))
        check(f"{pid}.{role}", router.resolve_model(pid, role), want or None)

# The seeded DeepSeek map is the documented default.
check("deepseek chat", router.resolve_model("deepseek", "chat"),
      "deepseek-v4-flash")
check("deepseek reasoning", router.resolve_model("deepseek", "reasoning"),
      "deepseek-v4-pro")
check("deepseek vision keeps local model",
      router.resolve_model("deepseek", "vision"), "microsoft/Florence-2-base")
# A provider with no role map must not change behaviour at all.
check("openai chat", router.resolve_model("openai", "chat"), "gpt-4o")

# ---- 3. stale-override validation -------------------------------------------
router._catalog_models = lambda pid: ["deepseek-v4-pro", "deepseek-chat"]
check("bogus override downgrades",
      router.resolve_model("deepseek", "chat"), "deepseek-v4-pro")
router._catalog_models = lambda pid: []
check("empty catalog keeps override",
      router.resolve_model("deepseek", "chat"), "deepseek-v4-flash")
router._catalog_models = saved_catalog

# ---- 4. shapes ---------------------------------------------------------------
check("describe shape", sorted(router.describe("deepseek").keys()),
      ["auto_route", "default_model", "provider", "roles", "route_validate"])
check("deepseek utility default", router.utility_model("deepseek"),
      "deepseek-chat")
check("a provider with no utility model returns None",
      router.utility_model("openai"), None)
check("for_provider resolves the utility role",
      router.for_provider(_Stub("deepseek"), "utility"), "deepseek-chat")
check("for_provider resolves the reasoning role",
      router.for_provider(_Stub("deepseek"), "reasoning"), "deepseek-v4-pro")
check("for_provider tolerates a provider with no id",
      router.for_provider(_Stub(""), "utility"), None)
check("for_provider tolerates junk",
      router.for_provider(object(), "utility"), None)

# An empty string saved by an older build must still resolve to the shipped
# default, not to nothing - otherwise the optimisation never reaches anyone who
# had already saved a setting.
saved_roles = config._data["providers"]["builtin"]["deepseek"].get("roles")
config._data["providers"]["builtin"]["deepseek"]["roles"] = {
    "chat": "", "reasoning": "", "vision": "", "utility": ""}
try:
    check("empty saved role falls back to the shipped default (chat)",
          router.resolve_model("deepseek", "chat"), "deepseek-v4-flash")
    check("empty saved role falls back to the shipped default (utility)",
          router.utility_model("deepseek"), "deepseek-chat")
    # A provider with no shipped roles is untouched by that fallback.
    check("no shipped role leaves the provider default alone",
          router.utility_model("openai"), None)
    check("no shipped role leaves resolve_model alone",
          router.resolve_model("openai", "chat"), "gpt-4o")
finally:
    config._data["providers"]["builtin"]["deepseek"]["roles"] = saved_roles

# A stale utility model degrades to the provider default instead of failing
# every background job.
router._catalog_models = lambda pid: ["deepseek-v4-pro", "deepseek-chat"]
config._data["providers"]["builtin"]["deepseek"]["roles"]["utility"] = "gone"
try:
    check("a stale utility model degrades to the provider default",
          router.utility_model("deepseek"), None)
finally:
    config._data["providers"]["builtin"]["deepseek"]["roles"]["utility"] = ""
    router._catalog_models = saved_catalog

# ---- 5. auto_route False => None everywhere ----------------------------------
providers = config._data.setdefault("providers", {})
providers["auto_route"] = False
try:
    check("disabled chat", router.resolve_model("deepseek", "chat"), None)
    check("disabled pick", router.pick("deepseek", "hi")[1], None)
    check("disabled utility", router.utility_model("deepseek"), None)
    check("disabled for_provider",
          router.for_provider(_Stub("deepseek"), "utility"), None)
finally:
    providers["auto_route"] = True

# ---- 6. never raises on junk -------------------------------------------------
check("empty message", router.classify(""), "chat")
check("none-ish message", router.classify(None), "chat")
check("unknown provider", router.resolve_model("nope", "chat"), None)

print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
