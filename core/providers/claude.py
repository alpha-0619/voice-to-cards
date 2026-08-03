"""The Claude provider.

Three settings here are load-bearing and each one is a decision, not a default:

**Thinking stays on, at low effort.** The instinct on a latency-critical path is
to turn thinking off. On this model that is a trap: with thinking disabled a
forced tool call can come back written into the visible response text instead of
as a tool call. The turn returns 200, nothing raises, and the tool simply never
runs -- in an agentic loop that phantom text then pollutes every later turn. Low
effort gets the same token savings without opening that door.

**No sampling parameters.** `temperature`, `top_p`, and `top_k` are rejected
outright on current models. Determinism comes from the forced tool call and a
closed action enum, not from a temperature of zero (which never guaranteed
identical output anyway).

**Retries off.** The SDK's default backoff on a throttle is tens of seconds,
which on screen is indistinguishable from a hang. Failing immediately lets the
caller show a recoverable state.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from ..config import get_settings
from ..prompt import ROUTER_TOOL
from .base import ArgsFragment, Completed, Failed, ProviderEvent


def _usage_of(message) -> dict:
    """Token counts, including the two fields that prove caching is working.

    A `cache_read` that stays at zero across identical-prefix requests means
    something is quietly changing the prefix -- that is the number to watch,
    not the wall clock.
    """
    usage = getattr(message, "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


class ClaudeProvider:
    name = "claude"

    def __init__(self) -> None:
        from anthropic import AsyncAnthropic

        self._settings = get_settings()
        self._client = AsyncAnthropic(
            api_key=self._settings.anthropic_api_key or None,
            max_retries=self._settings.max_retries,
            timeout=self._settings.request_timeout_s,
        )

    def _request(self, system: list[dict], tool: dict, messages: list[dict]) -> dict:
        s = self._settings
        return {
            "model": s.model,
            "max_tokens": s.max_tokens,
            "system": system,
            "thinking": {"type": s.thinking},
            "output_config": {"effort": s.effort},
            "tools": [tool],
            # One tool, forced, no parallel calls: the response is always our
            # structure. There is no free text to fail to parse.
            "tool_choice": {
                "type": "tool",
                "name": ROUTER_TOOL,
                "disable_parallel_tool_use": True,
            },
            "messages": messages,
        }

    async def stream(
        self,
        *,
        system: list[dict],
        tool: dict,
        messages: list[dict],
    ) -> AsyncIterator[ProviderEvent]:
        if self._settings.streaming:
            async for event in self._stream_live(system, tool, messages):
                yield event
        else:
            async for event in self._whole_response(system, tool, messages):
                yield event

    async def _stream_live(
        self, system: list[dict], tool: dict, messages: list[dict]
    ) -> AsyncIterator[ProviderEvent]:
        from anthropic import APIError

        try:
            async with self._client.messages.stream(**self._request(system, tool, messages)) as stream:
                async for event in stream:
                    # Thinking and text blocks are ignored on purpose: the only
                    # thing this path cares about is the tool's arguments, and
                    # they arrive as `input_json_delta` fragments.
                    if (
                        event.type == "content_block_delta"
                        and getattr(event.delta, "type", None) == "input_json_delta"
                    ):
                        yield ArgsFragment(event.delta.partial_json)
                final = await stream.get_final_message()
        except APIError as exc:
            yield Failed(kind=_classify(exc), detail=f"{type(exc).__name__}: {str(exc)[:200]}")
            return
        except Exception as exc:
            # Anything else -- an auth error the SDK defers to request time, a
            # DNS failure, a transport fault. The contract is that a provider
            # reports failure as a value; letting it raise here would tear an
            # already-open SSE stream and the client would see a truncated
            # response rather than an error it can act on.
            yield Failed(kind="unavailable", detail=f"{type(exc).__name__}: {str(exc)[:200]}")
            return

        yield Completed(usage=_usage_of(final), stop_reason=getattr(final, "stop_reason", None))

    async def _whole_response(
        self, system: list[dict], tool: dict, messages: list[dict]
    ) -> AsyncIterator[ProviderEvent]:
        """The non-streaming path, kept only so the two can be measured against
        each other. It re-serialises the finished arguments into a single
        fragment so everything downstream -- decoding, validation, chaining --
        runs identically on both paths.
        """
        from anthropic import APIError

        try:
            message = await self._client.messages.create(**self._request(system, tool, messages))
        except APIError as exc:
            yield Failed(kind=_classify(exc), detail=f"{type(exc).__name__}: {str(exc)[:200]}")
            return
        except Exception as exc:
            # Anything else -- an auth error the SDK defers to request time, a
            # DNS failure, a transport fault. The contract is that a provider
            # reports failure as a value; letting it raise here would tear an
            # already-open SSE stream and the client would see a truncated
            # response rather than an error it can act on.
            yield Failed(kind="unavailable", detail=f"{type(exc).__name__}: {str(exc)[:200]}")
            return

        block = next((b for b in message.content if b.type == "tool_use"), None)
        if block is None:
            # Forced tool choice makes this unreachable in normal operation. It
            # is exactly the shape a thinking-disabled run degrades into, so it
            # is reported rather than assumed away.
            yield Failed(kind="malformed", detail=f"no tool_use block (stop={message.stop_reason})")
            return

        yield ArgsFragment(json.dumps(block.input or {}))
        yield Completed(usage=_usage_of(message), stop_reason=message.stop_reason)


def _classify(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    if status == 429:
        return "throttled"
    if status is not None and status >= 500:
        return "unavailable"
    return "unavailable"
