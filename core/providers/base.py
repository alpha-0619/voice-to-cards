"""What a provider owes the router: a stream of argument fragments.

The contract is deliberately thin. A provider does not decide anything and does
not parse anything -- it produces the raw fragments of the forced tool call's
arguments, in order, and then reports how the turn ended. All decoding happens
once, in `core.streaming`, so the live path and the offline path cannot drift
apart: the scripted provider exercises the exact same scanner the real one does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class ArgsFragment:
    """A slice of the tool call's argument JSON, exactly as it came off the wire."""

    text: str


@dataclass(frozen=True)
class Completed:
    """The turn finished. `usage` carries token counts and cache effectiveness."""

    usage: dict = field(default_factory=dict)
    stop_reason: str | None = None


@dataclass(frozen=True)
class Failed:
    """The turn did not produce a decision.

    Surfaced as a value rather than raised so the caller can render a calm,
    recoverable state. `kind` is coarse on purpose: the UI distinguishes
    "busy, try again" from "something is wrong", and nothing finer than that.
    """

    kind: str  # "throttled" | "unavailable" | "malformed"
    detail: str = ""


ProviderEvent = ArgsFragment | Completed | Failed


@runtime_checkable
class Provider(Protocol):
    name: str

    def stream(
        self,
        *,
        system: list[dict],
        tool: dict,
        messages: list[dict],
    ) -> AsyncIterator[ProviderEvent]:
        ...
