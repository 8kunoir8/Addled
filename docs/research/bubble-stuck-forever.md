# Why a bubble's text stays on screen forever

## The report

The floating avatar's notification bubble keeps its text on screen long after
it should have gone — it looks frozen.

## Cause

`show_thinking()` shows the bubble with **no auto-dismiss timer at all**, so if
the turn never produces a reply, nothing can ever clear it.

`ChatBubble._show` arms the dismissal timer only when given a duration:

```python
def _show(self, anchor: QPoint, duration_s: int | None):
    ...
    if duration_s is not None:
        self._timer.start(int(duration_s * 1000))
    self.show()
```

`show_message` passes one (`8` to `30`s, scaled by length) and `show_decision`
passes `DECISION_TIMEOUT_S`. `show_thinking` passes `duration_s=None`:

```python
self._show(anchor, duration_s=None)      # chat_bubble.py:145
```

So "Thinking…" is shown and **no timer is armed**. It stays until something
replaces it — and the only thing that replaces it is the reply arriving:

```python
bubble.show_thinking(char_widget.frameGeometry().center())   # main.py:897
try:
    ...
    asyncio.run_coroutine_threadsafe(_handle_prompt(answer), _ws_loop)
```

```python
async def _handle_prompt(text: str):
    result = await run_chat_pipeline(text)     # <- if this RAISES, nothing follows
    reply = result.get("response", "")
    ...
    bridge.reply.emit(reply)                   # <- only reached on success
```

If `run_chat_pipeline` raises — a provider error, a timeout, a dropped
connection, any exception in the pipeline — the coroutine ends there.
`bridge.reply.emit` never runs, `_show_reply` is never called, and nothing else
in the app touches the bubble. "Thinking…" is on screen, with no timer, and
stays there for the rest of the session.

`_handle_prompt` is scheduled with `run_coroutine_threadsafe`, so a raise is
captured into the returned future and logged, not re-raised anywhere that could
notice the bubble is still up.

## Verified

Built a real `ChatBubble` offscreen and showed thinking, then simulated a turn
that raises before replying:

```
show_thinking() -> visible = True
  _timer active  = False   <- False means NO auto-dismiss

after 26s with no reply:
  visible = True   text = 'Thinking…'
  _timer active = False
  STUCK FOREVER: shown, no timer, nothing will ever clear it
```

## The same bug was already fixed once — for decisions only

`_show_decision_now` carries this comment (`chat_bubble.py`):

> `duration_s=None` armed no timer at all, and with `fade_out` and the click
> both refusing while a decision is open, an unanswered prompt had NO exit: the
> bubble stayed on top of the character for the rest of the session. The
> long-but-finite deadline is what keeps "do not dismiss this out from under the
> user" from becoming "the user cannot get rid of it".

So the defect was found, understood, and fixed for the decision path — which is
why decisions now use `DECISION_TIMEOUT_S`. `show_thinking` was left on
`duration_s=None` and never got the same treatment. It is the same bug in the
one place it was not fixed, which is why the fix should follow the same shape
rather than invent a new one.

## Why it is not simply "a long timer"

A thinking bubble legitimately has no fixed duration: a slow local model can
take minutes, and cutting it off at 30 seconds would hide a working turn. The
decision path's answer is the right one here too — a **long but finite**
deadline. The distinction is between *no exit* and *a generous exit*:

- Decisions: `DECISION_TIMEOUT_S` (120s), so a prompt cannot be lost but also
  cannot trap the screen.
- Thinking: needs the same. A turn that has produced nothing after several
  minutes is not still working in any useful sense, and a bubble that cannot be
  dismissed is worse than one that gives up and says so.

Two things a fix should also do, since the timer alone would only bound the
problem rather than solve it:

1. **Give the failure a way to reach the bubble.** `_handle_prompt` should
   report a failed turn to the bubble instead of ending silently, so the text
   changes to something true rather than the optimistic "Thinking…" sitting
   there until the deadline.
2. **A click already dismisses it — verified, not assumed.** `fade_out` is
   refused only while a *decision* is open, and "Thinking…" is not one, so a
   click does clear it (measured offscreen: visible → gone in 0.2s). That means
   the bug is not a total trap, which is worth stating: the text is *stuck*,
   not *unremovable*. But it only helps a user who thinks to click a bubble
   that is telling them to wait, and nothing on screen says so. The timeout is
   still the fix; this is why it is not an emergency.

## Fixed

Implemented and deployed.

**`backend/character/chat_bubble.py`**
- `THINKING_TIMEOUT_S = 300`, next to `DECISION_TIMEOUT_S`, with the reasoning
  above recorded on the constant.
- `show_thinking` passes it instead of `None`.
- `_fade_step` clears `_thinking` when the bubble fully hides, because a
  timed-out "Thinking…" otherwise left the flag set and the next bubble painted
  with the thinking colours and the italic grey label.

**`backend/main.py`**
- `_handle_prompt` wraps `run_chat_pipeline` and, on a raise, emits
  `[Turn failed: <type>] <message>` to the bubble before returning. That is
  what should have been on screen all along, rather than "Thinking…" waiting
  for a reply that was never coming.

**`scripts/check_chat_bubble.py`** pins the rule down — every bubble that shows
is either dismissed by its own deadline or has a path that replaces it — and
also re-checks that messages and decisions keep theirs, so a future change
cannot fix one and quietly drop another.

### Verification

- Both files parse; the affected checks in the character suite pass.
- **Teeth.** Restoring `duration_s=None` fails *it arms the auto-dismiss timer*
  and *the timer was armed with exactly that deadline* by name. Unwrapping the
  pipeline call fails *and reports the failure to the bubble on the way out*
  and *the failure path returns* by name. Both restored afterwards, with no
  mutation markers left in the tree.
- **Offscreen, end to end.** Thinking shows with a 300s timer armed; a
  simulated failing turn puts `[Turn failed: RuntimeError] …` on the bubble
  instead of "Thinking…"; the bubble then clears in 0.4s.
- **Live.** Deployed to `C:/Program Files/Addled`, backend restarted, and a real
  turn driven over `ws://127.0.0.1:9876` returned its reply — the reply that
  replaces "Thinking…" on the real bubble. The installed `chat_bubble.py`
  carries `THINKING_TIMEOUT_S = 300` and the installed `main.py` carries the
  `[Turn failed:` emit.
- Not forced live: an actual raise from `run_chat_pipeline`. The live provider
  is healthy, and breaking the config to manufacture one was not worth it; that
  path is covered by the teeth test rather than by observation. **UNVERIFIED on
  the live app.**

## Scope

Only `show_thinking` is affected. `show_message` and `show_decision` both pass
a duration, and were tested to clear themselves:

- `show_message`: fades and hides (verified offscreen; opacity reaches 0,
  `hide()` called, `visible=False`).
- `show_decision`: `_timer` armed at `DECISION_TIMEOUT_S` (120000 ms, observed).
- Repeated messages arriving in sequence: each clears itself; a later message
  does not strand an earlier one.
