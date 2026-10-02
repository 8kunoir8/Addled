"""
Clarification questions the model can put to the user.

`ask_user` lets a turn stop and ask when it genuinely cannot tell what was
meant — which of two files, which account, which of three readings of a short
request — instead of guessing and producing work that has to be undone.

Two modules:

* ``policy`` — who may ask, for how long, and what counts as asking twice.
  The rules live here rather than at the call sites so a caller cannot opt out
  of them by forgetting to check.
* ``pending`` — the questions currently waiting, with their origin and expiry.

The turn does **not** wait for an answer. A question ends the turn, the card
stays until it is answered or lapses, and the answer arrives as the next turn.
That matches how approvals already work, and for the same reason: a local model
routinely outlived the old in-band wait, so a blocked turn looked like a hang.
"""

from __future__ import annotations

from backend.questions import pending, policy

__all__ = ["pending", "policy"]
