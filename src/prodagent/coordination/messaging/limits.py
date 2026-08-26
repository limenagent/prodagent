"""Single home for the message plane's public-text admission bounds.

Every boundary an agent-produced string can cross — a floor turn entering the
shared transcript, a handoff's prior output, a board value, a worker's error
text — is bounded so one verbose producer cannot blow a consumer's context
window. Two knobs, one per concept:

- ``PUBLIC_TURN_TEXT_MAX_CHARS`` — live-speech text on a shared floor. The
  admission bound and the projection cap (``PublicTextOnly``) must agree:
  the transcript itself and every member's view of it are bounded the same.
- ``CROSSING_OUTPUT_MAX_CHARS`` — free-text *output* crossing an agent
  boundary (default for ``HandoffPacket.prior_output_max_chars``, board
  values, worker error text). Mirrors the configurable
  ``OrchestrationConfig.handoff_output_max_chars``, which stays the knob for
  callers that plumb framework config; this is its default value.

Admission code should reference these names, not re-declare literals — the
"one boundary, one bound" invariant only holds if the numbers live once.
"""

from __future__ import annotations

__all__ = [
    "PUBLIC_TURN_TEXT_MAX_CHARS",
    "CROSSING_OUTPUT_MAX_CHARS",
]

PUBLIC_TURN_TEXT_MAX_CHARS = 4000
CROSSING_OUTPUT_MAX_CHARS = 2000
