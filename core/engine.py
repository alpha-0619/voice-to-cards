"""One utterance in, one card out -- emitted in pieces as it becomes known.

    utterance
      -> forced single-call routing        (core.prompt + a provider)
      -> partial decode                    (core.streaming)
      -> identifier validation             (the scenario's own patterns)
      -> tool call + follow-up chain       (the scenario's fixtures)
      -> a typed card

The engine knows nothing about any industry. Every domain fact -- what tools
exist, what an identifier looks like, which follow-up calls fill out a card,
what the refusal sentence says -- comes from the loaded pack. Swapping packs
swaps all of it without touching this file, which is the claim
`tests/test_scenario_equivalence.py` exists to keep honest.

Callers consume an async stream of events. The first useful one lands long
before the last: `UnderstoodDelta` carries the restatement as it is written,
so there is something true on screen while the tool call is still forming.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .prompt import build_router_tool, build_system_blocks
from .scenario import Scenario
from .streaming import FieldDelta, FieldDone, PartialJSONObject
from .providers.base import ArgsFragment, Completed, Failed

BUILTIN_URGENT = "flag_urgent"
BUILTIN_REFUSE = "refuse_off_topic"
BUILTIN_POLICY = "answer_policy"
BUILTIN_GENERAL = "answer_general"


# -- events ---------------------------------------------------------------


@dataclass(frozen=True)
class Trace:
    """A step, timestamped from the start of the turn.

    Not decoration: these are the numbers the latency work is argued from, and
    they are the same ones the bench harness reads.
    """

    stage: str
    t_ms: int
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class UnderstoodDelta:
    text: str


@dataclass(frozen=True)
class Understood:
    text: str
    language: str


@dataclass(frozen=True)
class Routed:
    action: str
    arguments: dict


@dataclass(frozen=True)
class CardReady:
    card: str
    layout: str
    data: dict
    actions: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ReplyReady:
    text: str
    sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EngineError:
    kind: str
    message: str
    actions: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Done:
    usage: dict = field(default_factory=dict)


EngineEvent = (
    Trace | UnderstoodDelta | Understood | Routed | CardReady | ReplyReady | EngineError | Done
)


# -- engine ---------------------------------------------------------------


class Engine:
    def __init__(self, scenario: Scenario, provider: Any) -> None:
        self.scenario = scenario
        self.provider = provider
        # Assembled once. Rebuilding per request would change the prefix bytes
        # and cost a cache write every turn.
        self._system = build_system_blocks(scenario)
        self._tool = build_router_tool(scenario)

    async def run(
        self,
        utterance: str,
        *,
        history: list[dict] | None = None,
    ) -> AsyncIterator[EngineEvent]:
        started = time.monotonic()

        def ms() -> int:
            return int((time.monotonic() - started) * 1000)

        yield Trace("utterance", ms(), {"text": utterance})

        messages = list(history or []) + [{"role": "user", "content": utterance}]
        parser = PartialJSONObject()
        decision: dict[str, Any] = {}
        usage: dict = {}
        failure: Failed | None = None
        emitted_understood = False
        first_text_ms: int | None = None

        try:
            async for event in self.provider.stream(
                system=self._system, tool=self._tool, messages=messages
            ):
                if isinstance(event, ArgsFragment):
                    for parsed in parser.feed(event.text):
                        if isinstance(parsed, FieldDelta):
                            if parsed.key == "understood":
                                if first_text_ms is None:
                                    first_text_ms = ms()
                                    yield Trace("first_text", first_text_ms, {})
                                yield UnderstoodDelta(parsed.text)
                        elif isinstance(parsed, FieldDone):
                            decision[parsed.key] = parsed.value
                            # Emit as soon as both halves of the restatement
                            # exist, rather than waiting for the whole decision.
                            if (
                                not emitted_understood
                                and "understood" in decision
                                and "language" in decision
                            ):
                                emitted_understood = True
                                yield Understood(
                                    str(decision["understood"]),
                                    str(decision["language"])[:2].lower(),
                                )
                elif isinstance(event, Completed):
                    usage = event.usage
                elif isinstance(event, Failed):
                    failure = event
                    break
        except ValueError as exc:
            # Malformed JSON: the scanner rejected something it could not decode.
            failure = Failed(kind="malformed", detail=str(exc))

        if failure is None:
            # A *truncated* stream is the quieter failure: nothing is malformed,
            # the object simply never closed. Without this check the decision
            # dict is merely incomplete, `action` falls back to a refusal, and a
            # dropped connection renders as a polite card instead of an error.
            try:
                for parsed in parser.close():
                    if isinstance(parsed, FieldDone):
                        decision[parsed.key] = parsed.value
            except ValueError as exc:
                failure = Failed(kind="malformed", detail=str(exc))
            if failure is None and not parser.done:
                failure = Failed(
                    kind="malformed",
                    detail="stream ended before the decision was complete",
                )

        if failure is not None:
            yield Trace("failed", ms(), {"kind": failure.kind, "detail": failure.detail})
            yield self._error_event(failure.kind, decision.get("language"))
            yield Done(usage)
            return

        language = str(decision.get("language") or self.scenario.spec.default_language)[:2].lower()
        if not emitted_understood:
            # Reached when the model omitted `language`; the restatement is
            # still usable, so fall back rather than discarding the turn.
            yield Understood(str(decision.get("understood") or ""), language)

        action = str(decision.get("action") or BUILTIN_REFUSE)
        raw_args = decision.get("arguments")
        arguments = self._clean_arguments(raw_args if isinstance(raw_args, dict) else {})

        yield Trace(
            "routed",
            ms(),
            {"action": action, "arguments": arguments, "language": language, **usage},
        )
        yield Routed(action, arguments)

        async for event in self._act(action, arguments, decision, language, ms):
            yield event

        yield Done(usage)

    # -- dispatch ---------------------------------------------------------

    async def _act(
        self,
        action: str,
        arguments: dict,
        decision: dict,
        language: str,
        ms,
    ) -> AsyncIterator[EngineEvent]:
        scenario = self.scenario

        if action == BUILTIN_URGENT:
            yield CardReady(
                card="urgent",
                layout="urgent",
                data={"message": scenario.text("urgent_message", language)},
                actions=[
                    {"id": "call_staff", "label": scenario.text("urgent_primary", language),
                     "variant": "danger"},
                    {"id": "guide", "label": scenario.text("urgent_secondary", language),
                     "variant": "danger"},
                ],
            )
            return

        if action in (BUILTIN_POLICY, BUILTIN_GENERAL):
            reply = str(decision.get("reply") or "").strip()
            if not reply:
                # An empty reply is a routing failure, not an answer. Fall
                # through to the refusal rather than render a blank card.
                yield self._refusal(language)
                return
            raw_sources = decision.get("sources")
            sources = (
                [str(s).strip() for s in raw_sources if str(s).strip()][:4]
                if isinstance(raw_sources, list) and action == BUILTIN_POLICY
                else []
            )
            yield ReplyReady(reply, sources)
            return

        tool = scenario.spec.tool(action)
        if tool is None:
            yield self._refusal(language)
            return

        try:
            result = scenario.fixture(tool.name, language)
            yield Trace("tool_result", ms(), {"tool": tool.name, "result": result})
            for step_tool, step_args, step_result in self._run_chain(tool.name, arguments, result, language):
                yield Trace(
                    "chain_result",
                    ms(),
                    {"tool": step_tool, "arguments": step_args, "result": step_result},
                )
                # The originating call wins on conflict: a follow-up exists to
                # fill gaps, not to overwrite the answer that was asked for.
                result = {**step_result, **result}
        except KeyError as exc:
            yield Trace("failed", ms(), {"kind": "unavailable", "detail": str(exc)})
            yield self._error_event("unavailable", language)
            return

        card = scenario.spec.cards[tool.card]
        yield Trace("render", ms(), {"card": tool.card, "layout": card.layout})
        yield CardReady(card=tool.card, layout=card.layout, data=result)

    # -- helpers ----------------------------------------------------------

    def _clean_arguments(self, raw: dict) -> dict:
        """Drop placeholders and anything that fails the pack's own format.

        Two different failures, one policy. A model told to omit unknown values
        still occasionally emits `"<UNKNOWN>"`; and a plausible-looking but
        invented identifier is the more dangerous case, because it will resolve
        against *someone's* record once this is wired to a live system. Both are
        dropped. A card missing a section is recoverable; a card built from the
        wrong person's identifier is not.
        """
        cleaned: dict = {}
        for key, value in raw.items():
            if isinstance(value, str):
                text = value.strip()
                if not text or text.startswith("<") or text.lower() in {
                    "unknown", "n/a", "none", "null", "tbd",
                }:
                    continue
                if not self.scenario.valid_identifier(key, text):
                    continue
                cleaned[key] = text
            elif value is not None:
                cleaned[key] = value
        return cleaned

    def _run_chain(self, tool_name: str, arguments: dict, result: dict, language: str):
        """Follow-up calls that complete a card.

        A step whose arguments cannot be resolved from the current call is
        skipped, never defaulted. This is the whole reason the resolver exists:
        the tempting shortcut is `args.get("booking_ref") or "<the demo one>"`,
        which works beautifully against fixtures and quietly queries a stranger's
        booking the day real data is connected.
        """
        for rule in self.scenario.spec.chains:
            if not rule.matches(tool_name, result):
                continue
            for step in rule.then:
                resolved: dict[str, Any] = {}
                skip = False
                for key, expr in step.args.items():
                    value = self._resolve(expr, arguments, result, language)
                    if value is None:
                        skip = True
                        break
                    resolved[key] = value
                if skip:
                    continue
                try:
                    yield step.tool, resolved, self.scenario.fixture(step.tool, language)
                except KeyError:
                    continue

    def _resolve(self, expr: str, arguments: dict, result: dict, language: str) -> Any:
        """`$args.x | $result.y | literal` -- first non-empty wins, else None."""
        for part in str(expr).split("|"):
            part = part.strip()
            if part.startswith("$args."):
                value = arguments.get(part[len("$args.") :])
            elif part.startswith("$result."):
                value = result.get(part[len("$result.") :])
            elif part.startswith("$strings."):
                value = self.scenario.text(part[len("$strings.") :], language)
            else:
                value = part
            if value not in (None, ""):
                return value
        return None

    def _refusal(self, language: str) -> CardReady:
        scenario = self.scenario
        return CardReady(
            card="refusal",
            layout="notice",
            data={"message": scenario.text("refusal_message", language)},
            actions=[
                {"id": "retry", "label": scenario.text("retry", language), "variant": "primary"}
            ],
        )

    def _error_event(self, kind: str, language: str | None) -> EngineError:
        lang = (language or self.scenario.spec.default_language)[:2]
        key = "busy_message" if kind == "throttled" else "error_message"
        return EngineError(
            kind=kind,
            message=self.scenario.text(key, lang),
            actions=[
                {"id": "retry", "label": self.scenario.text("retry", lang), "variant": "primary"}
            ],
        )
