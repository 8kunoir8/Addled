"""Do the OpenAI-shaped providers honour a thinking model's reply correctly?

Two faults, both found with the local qwen3-8b on port 8090:

1. `reasoning_content` was DROPPED. The field exists on ProviderResult and
   `deepseek_provider` fills it, but `openai_provider` did not — and
   `LocalProvider` subclasses that one, so every local thinking model lost it.
   `tool_loop` must echo it back on the follow-up request.

2. An empty reply came back as `ok=True, response=""`. That is the silent
   failure this codebase keeps rediscovering: the caller sees a provider that
   worked, gets nothing, and reports a misleading symptom somewhere else. A
   thinking model that spends its whole budget reasoning is the common cause.

The reply is parsed with a stub HTTP client, so this needs no server and no
network: it is about how the RESPONSE is interpreted, not about any model.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\Kunoir\Codeground\Clicky\Addled")

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


from backend.providers.openai_provider import OpenAIProvider

REPLY_TEMPLATE = {
    "model": "stub-model",
    "choices": [{
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": ""},
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
}


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _Resp(self._payload)


def _provider(payload):
    p = OpenAIProvider({"api_key": "sk-test", "base_url": "http://stub/v1",
                        "default_model": "stub-model"})
    p._get_client = lambda: _Client(payload)   # type: ignore[assignment]
    return p


def call(payload, max_tokens=2000):
    return asyncio.run(_provider(payload).chat(
        [{"role": "user", "content": "hi"}], max_tokens=max_tokens))


def reply(**message):
    d = json.loads(json.dumps(REPLY_TEMPLATE))
    d["choices"][0]["message"].update(message)
    return d


print("=== a normal reply ===")
r = call(reply(content="blue chair"))
check("it succeeds", r.ok is True, str(r.error))
check("the content is returned", r.response == "blue chair", r.response)

print()
print("=== reasoning_content is carried through ===")
r = call(reply(content="answer here", reasoning_content="I thought about it"))
check("ok", r.ok is True, str(r.error))
check("reasoning_content is POPULATED, not dropped",
      r.reasoning_content == "I thought about it", repr(r.reasoning_content))
check("the answer is still the content, not the reasoning",
      r.response == "answer here", r.response)

print()
print("=== a thinking model that spent its budget ===")
# The real shape from qwen3-8b at 2000 tokens: finish_reason "length", thousands
# of characters of reasoning, no content.
r = call({"model": "stub-model",
          "choices": [{"finish_reason": "length",
                       "message": {"role": "assistant", "content": "",
                                   "reasoning_content": "x" * 4000}}],
          "usage": {"prompt_tokens": 10, "completion_tokens": 2000}},
         max_tokens=2000)
check("an empty, truncated reply is a FAILURE, not an empty success",
      r.ok is False, f"ok={r.ok} response={r.response[:60]!r}")
check("the message explains it ran out of room thinking",
      "thinking" in str(r.error).lower() and "2000" in str(r.error),
      str(r.error))
check("it says what to do", "raise the limit" in str(r.error).lower()
      or "thinking mode" in str(r.error).lower(), str(r.error))
check("the reasoning is still kept, so the truncation can be inspected",
      len(r.reasoning_content) == 4000, str(len(r.reasoning_content)))

print()
print("=== reasoning is NOT passed off as the answer ===")
# The reasoning is prose ("Okay, so the user is asking..."). Handing it back as
# `response` made the forge treat a sentence as code and report a syntax error
# about it, which is the misleading symptom this whole area keeps producing.
r = call(reply(content="", reasoning_content="Okay, so the user is asking..."))
check("an empty content is not replaced by the reasoning text",
      r.response == "" or "Okay, so the user" not in r.response,
      repr(r.response[:80]))
check("and it is reported as a failure", r.ok is False, f"ok={r.ok}")

print()
print("=== a genuinely empty reply says so plainly ===")
r = call(reply(content=""))
check("empty with no reasoning is a failure", r.ok is False, f"ok={r.ok}")
check("the message does not blame thinking",
      "thinking" not in str(r.error).lower(), str(r.error))
check("the message is usable", "empty response" in str(r.error).lower(),
      str(r.error))

print()
print("=== errors are never blank ===")
# `str(httpx.ReadTimeout())` is EMPTY, so the caller saw "Provider error: " with
# nothing after it and blamed the model for a timeout it could not see.
import httpx  # noqa: E402

p = _provider(json.loads(json.dumps(REPLY_TEMPLATE)))


class _Boom:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        raise httpx.ReadTimeout("")


p._get_client = lambda: _Boom()  # type: ignore[assignment]
r = asyncio.run(p.chat([{"role": "user", "content": "hi"}], max_tokens=100))
check("a timeout is a failure", r.ok is False, f"ok={r.ok}")
check("the message is NOT empty", bool(str(r.error).strip()), repr(r.error))
check("the message names the timeout",
      "did not finish within" in str(r.error).lower()
      or "timeout" in str(r.error).lower(), str(r.error))
check("it says how to raise the limit",
      "ADDLED_HTTP_TIMEOUT" in str(r.error), str(r.error))

# An exception with no message must still produce a usable reason.
class _Boom2(_Boom):
    async def post(self, *a, **k):
        raise RuntimeError("")


p2 = _provider(json.loads(json.dumps(REPLY_TEMPLATE)))
p2._get_client = lambda: _Boom2()  # type: ignore[assignment]
r2 = asyncio.run(p2.chat([{"role": "user", "content": "hi"}], max_tokens=100))
check("an exception with no message still yields a reason",
      r2.ok is False and bool(str(r2.error).strip()), repr(r2.error))
check("and it names the exception type",
      "RuntimeError" in str(r2.error), str(r2.error))

print()
print("=== local providers get a longer timeout ===")
for pid, expect_local in (("local", True), ("ollama", True),
                          ("lmstudio", True), ("openai", False),
                          ("deepseek", False)):
    p3 = OpenAIProvider({"api_key": "k", "base_url": "http://x/v1",
                         "provider_id": pid})
    t = p3._timeout()
    ok = (t >= 300) if expect_local else (t < 300)
    check(f"{pid} timeout is {'long' if expect_local else 'normal'} ({t:.0f}s)",
          ok, f"got {t}s")

print()
print("=== deepseek_provider still sets it (the pattern being matched) ===")
src = Path(r"E:\Kunoir\Codeground\Clicky\Addled\backend\providers"
           r"\deepseek_provider.py").read_text(encoding="utf-8")
check("deepseek still passes reasoning_content",
      "reasoning_content=message.get(" in src,
      "the provider this one was aligned to has changed")
oai = Path(r"E:\Kunoir\Codeground\Clicky\Addled\backend\providers"
           r"\openai_provider.py").read_text(encoding="utf-8")
check("openai_provider now does too",
      "reasoning_content=reasoning" in oai,
      "the field is still not populated")

print()
print("FAILED: " + ", ".join(fails) if fails
      else "all thinking-model provider checks passed")
raise SystemExit(1 if fails else 0)
