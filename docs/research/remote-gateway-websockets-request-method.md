# The remote gateway calls `request.method`, which does not exist

Found on 2026-10-10 while diagnosing `check_remote_gateway`. Not fixed; recorded
here with the reproduction because it is a real bug in shipped code and the fix
needs a decision.

## The defect

`backend/remote/gateway.py:284`:

```python
method = (request.method or "GET").upper()
```

Imported at the top of the same file:

```python
from websockets.asyncio.server import serve
```

That is the **modern** websockets API. But the app deliberately runs
`websockets` **15.0.1** from `pylibs`, not the bundled 17.0.1 — `app_paths.py`
pins it because `google_genai` requires `websockets <17.0`. And on 15.0.1 the
`Request` object has no `method` field at all:

```
Request public attrs: ['exception', 'parse', 'serialize']
init sig: (path: str, headers: Headers, _exception: Exception | None = None)
has path: True    has method: False    has headers: True
```

`path` and `headers` exist; `method` does not.

## Reproduction

```python
# with the app's own path setup, so websockets is the 15.0.1 the app uses
from backend import app_paths; app_paths.add_pylibs_to_path()
from websockets.asyncio.server import serve

def process_request(conn, request):
    print("attrs:", [a for a in ("path", "method", "headers")
                     if hasattr(request, a)])
    return None

server = await serve(handler, "127.0.0.1", 0, process_request=process_request)
# -> attrs: ['path', 'headers']      (no 'method')
```

`_process_request` then raises `AttributeError`, which its own `except` logs as
`Gateway request failed: 'Request' object has no attribute 'method'` and
returns nothing — so the request dies with no response rather than a clear
error.

## Why it has not been noticed

The remote gateway is **off** by default and disabled on this machine
(`Remote access armed (gateway off)`). It needs `remote.enabled` plus a
password before it will start, so nobody has exercised this path. It would fire
on the first request once remote access is turned on.

## The decision needed

The method is not lost — it is in the headers. On 15.0.1, `request.headers`
is a `Headers` object, and a plain HTTP request reaching `process_request` has
no WebSocket upgrade. Options:

1. Read it from the request line / headers rather than a `method` attribute.
2. `getattr(request, "method", None) or "GET"` — treats every request as GET,
   which is wrong for the `/api/` paths that check for POST.
3. Pin and use the bundled websockets API consistently, if `google_genai` no
   longer forces `<17.0`.

Option 3 is the only one that removes the mismatch rather than working around
it, but it changes a dependency the vision/browser installers resolve together,
so it is not a change to make unattended.

## Also note

`requirements.txt` says `websockets>=12.0` with no upper bound, so a user
environment can install a version where this breaks differently again. Whatever
is chosen should come with a bound and a check that exercises the gateway.
