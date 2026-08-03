"""A provider that never touches the network.

Two jobs, and the second one is why it exists at all.

**Tests.** Every layer above this -- decoding, identifier validation, chaining,
card assembly, the SSE encoding -- is exercised without an API key, so the suite
runs in CI and fails for real reasons rather than flaky ones.

**Demonstrations.** Canned utterances are replayed through the identical
pipeline in about a millisecond, with no request leaving the box. That matters
for the reason you find out the hard way: a live demo in front of an audience
should not be able to fail because of someone else's rate limit, and the canned
path deliberately sits outside the request guards, so the demo keeps working
even after the day's ceiling is spent.

Fragments are emitted in small, deliberately awkward slices -- boundaries land
mid-token and mid-escape -- so the offline path stresses the scanner rather than
handing it one tidy payload.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from .base import ArgsFragment, Completed, ProviderEvent

DEFAULT_CHUNK = 7


class ScriptedProvider:
    """Replays decisions supplied by the caller, in provider-shaped events."""

    name = "scripted"

    def __init__(self, chunk_size: int = DEFAULT_CHUNK) -> None:
        self._chunk_size = max(1, chunk_size)
        self._queue: list[dict] = []

    def queue(self, decision: dict) -> "ScriptedProvider":
        """Add one decision to be returned by the next call to `stream`."""
        self._queue.append(decision)
        return self

    async def stream(
        self,
        *,
        system: list[dict],
        tool: dict,
        messages: list[dict],
    ) -> AsyncIterator[ProviderEvent]:
        decision = self._queue.pop(0) if self._queue else _echo(messages)
        # ensure_ascii keeps non-Latin text as \uXXXX escapes, so the offline
        # path exercises escape handling instead of quietly avoiding it.
        payload = json.dumps(decision, ensure_ascii=True)
        for i in range(0, len(payload), self._chunk_size):
            yield ArgsFragment(payload[i : i + self._chunk_size])
        yield Completed(
            usage={"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0},
            stop_reason="tool_use",
        )


def _echo(messages: list[dict]) -> dict:
    """Fallback when nothing was queued: refuse rather than invent a routing.

    A scripted provider that guesses would let a test pass for the wrong reason.
    """
    last = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    return {
        "understood": f"You said: {last}",
        "language": "en",
        "action": "refuse_off_topic",
        "arguments": {},
    }
