"""Engine behaviour, driven entirely offline.

The interesting cases here are the ones where doing the *obvious* thing would
produce a confident, wrong answer: filling a missing identifier from an earlier
turn, accepting an identifier the model invented, or rendering a card built from
half a decision. Each of those has a test that fails if the guard is removed.
"""

from __future__ import annotations

import pytest

from core.engine import (
    CardReady,
    Done,
    EngineError,
    Engine,
    ReplyReady,
    Routed,
    Trace,
    Understood,
    UnderstoodDelta,
)
from core.providers.base import ArgsFragment, Failed
from core.providers.scripted import ScriptedProvider
from core.scenario import load_scenario


@pytest.fixture
def scenario():
    return load_scenario("airport")


async def run(scenario, decision, utterance="...", history=None, chunk_size=7):
    provider = ScriptedProvider(chunk_size=chunk_size).queue(decision)
    engine = Engine(scenario, provider)
    return [e async for e in engine.run(utterance, history=history)]


def only(events, kind):
    return [e for e in events if isinstance(e, kind)]


def card(events) -> CardReady:
    found = only(events, CardReady)
    assert found, f"no card in {[type(e).__name__ for e in events]}"
    return found[0]


# -- the streaming contract ------------------------------------------------


async def test_restatement_streams_before_the_card_exists(scenario):
    """The reason for the whole design: readable text lands first."""
    events = await run(
        scenario,
        {
            "understood": "You want to know if flight SX412 is delayed.",
            "language": "en",
            "action": "get_flight_status",
            "arguments": {"flight_number": "SX412"},
        },
        chunk_size=1,
    )
    first_delta = next(i for i, e in enumerate(events) if isinstance(e, UnderstoodDelta))
    first_card = next(i for i, e in enumerate(events) if isinstance(e, CardReady))
    assert first_delta < first_card

    streamed = "".join(e.text for e in only(events, UnderstoodDelta))
    assert streamed == "You want to know if flight SX412 is delayed."
    assert only(events, Understood)[0].text == streamed


async def test_first_text_trace_is_emitted_for_measurement(scenario):
    events = await run(
        scenario,
        {"understood": "Hello.", "language": "en", "action": "refuse_off_topic", "arguments": {}},
    )
    stages = [e.stage for e in only(events, Trace)]
    assert "first_text" in stages


# -- chaining and the missing-identifier rule ------------------------------


async def test_delay_chain_completes_the_card(scenario):
    """A delay answers three questions at once when it can."""
    events = await run(
        scenario,
        {
            "understood": "You want to know about SX412.",
            "language": "en",
            "action": "get_flight_status",
            "arguments": {"flight_number": "SX412", "booking_ref": "SX7K2Q4"},
        },
    )
    data = card(events).data
    assert data["status"] == "DELAYED"
    assert data["gate"] == "C14"           # from get_gate_info
    assert data["voucher_code"] == "SX-MEAL-4471"  # from issue_meal_voucher


async def test_chain_step_is_skipped_when_its_argument_is_missing(scenario):
    """No booking reference means no voucher -- not a voucher for someone else.

    This is the guard that matters most the day fixtures are swapped for a live
    system: a hardcoded fallback identifier here would issue a real voucher
    against a stranger's booking and nothing would look wrong.
    """
    events = await run(
        scenario,
        {
            "understood": "You want to know about SX412.",
            "language": "en",
            "action": "get_flight_status",
            "arguments": {"flight_number": "SX412"},  # no booking_ref
        },
    )
    data = card(events).data
    assert data["status"] == "DELAYED"
    assert data["gate"] == "C14"           # this step still resolves
    assert "voucher_code" not in data      # this one cannot, so it did not run


async def test_card_data_is_projected_onto_what_the_card_declares(scenario):
    """A chain routinely returns more than the card it is filling can show.

    Walking directions carry a destination and a step list; a baggage card has
    rows for the belt and the walk but not for the destination, which on that
    card would just repeat the hall. Passing the whole merged dict through means
    the interface silently ignores the surplus, which is the same invisible
    failure as a renamed field. Projecting makes the card spec the contract in
    both directions, and the drop is traced rather than silent.
    """
    events = await run(
        scenario,
        {
            "understood": "Where are my bags?",
            "language": "en",
            "action": "get_bag_belt",
            "arguments": {"flight_number": "SX412"},
        },
    )
    spec = scenario.spec.cards["detail"]
    declared = set(spec.fields) | set(spec.optional_fields)

    data = card(events).data
    assert set(data) <= declared, f"undeclared keys reached the client: {set(data) - declared}"
    assert data["belt"] == "6"          # the answer
    assert data["steps"]               # and the walk, because `detail` declares it

    dropped = [t for t in only(events, Trace) if t.stage == "dropped"]
    assert "destination" in (dropped[0].detail["fields"] if dropped else []), (
        "a dropped field must be traced, not discarded quietly"
    )


async def test_chain_does_not_overwrite_the_primary_answer(scenario):
    """A follow-up fills gaps; it never wins a conflict.

    `destination` is the case that makes this concrete, and it is not
    contrived -- the two tools legitimately mean different things by the same
    word. `get_connection_risk` returns where the passenger is *going* (SIN);
    the chained `get_directions` returns where they are *walking* (Gate D22).
    Merge the wrong way round and the card cheerfully tells someone their
    flight to Singapore now terminates at gate D22.
    """
    events = await run(
        scenario,
        {
            "understood": "Tight connection.",
            "language": "en",
            "action": "get_connection_risk",
            "arguments": {"booking_ref": "SX7K2Q4"},
        },
    )
    data = card(events).data
    assert data["onward_gate"] == "D22"
    assert data["held_seat"] == "14C"      # protect_connection ran
    assert data["walk_minutes"] == 7       # get_directions ran
    assert data["destination"] == "SIN"    # and did not redefine the trip


# -- identifier hygiene ----------------------------------------------------


@pytest.mark.parametrize(
    "bogus", ["<UNKNOWN>", "N/A", "none", "  ", "TK45BX2", "SX", "7K2Q4", "SX7K2Q4EXTRA"]
)
async def test_bad_identifiers_never_reach_a_tool(scenario, bogus):
    """Placeholders and wrong-shaped references are dropped, not forwarded."""
    events = await run(
        scenario,
        {
            "understood": "Check me in.",
            "language": "en",
            "action": "check_in",
            "arguments": {"booking_ref": bogus},
        },
    )
    assert only(events, Routed)[0].arguments == {}


async def test_well_formed_identifier_survives(scenario):
    events = await run(
        scenario,
        {
            "understood": "Check me in.",
            "language": "en",
            "action": "check_in",
            "arguments": {"booking_ref": "SX7K2Q4"},
        },
    )
    assert only(events, Routed)[0].arguments == {"booking_ref": "SX7K2Q4"}


async def test_unconstrained_arguments_pass_through(scenario):
    """Only fields the pack declares a pattern for are shape-checked."""
    events = await run(
        scenario,
        {
            "understood": "Where is the lounge?",
            "language": "en",
            "action": "get_directions",
            "arguments": {"destination": "the lounge"},
        },
    )
    assert only(events, Routed)[0].arguments == {"destination": "the lounge"}


# -- language --------------------------------------------------------------


async def test_spanish_reaches_both_the_fixture_and_the_engine_text(scenario):
    """Localisation is pre-translated on both sides of the boundary: the tool's
    human-readable fields and the sentences the engine writes itself."""
    events = await run(
        scenario,
        {
            "understood": "Quiere saber sobre el vuelo SX412.",
            "language": "es",
            "action": "get_flight_status",
            "arguments": {"flight_number": "SX412"},
        },
    )
    assert "Retraso" in card(events).data["reason"]

    refused = await run(
        scenario,
        {"understood": "...", "language": "es", "action": "refuse_off_topic", "arguments": {}},
    )
    assert "Solstice" in card(refused).data["message"]
    assert card(refused).actions[0]["label"] == "Reintentar"


async def test_unknown_language_falls_back_without_failing(scenario):
    events = await run(
        scenario,
        {"understood": "...", "language": "ja", "action": "refuse_off_topic", "arguments": {}},
    )
    assert card(events).actions[0]["label"] == "Try again"


# -- built-in actions ------------------------------------------------------


async def test_urgent_renders_handover_buttons(scenario):
    events = await run(
        scenario,
        {"understood": "Wallet gone.", "language": "en", "action": "flag_urgent", "arguments": {}},
    )
    c = card(events)
    assert c.layout == "urgent"
    assert [a["id"] for a in c.actions] == ["call_staff", "guide"]


async def test_policy_answer_carries_its_sources(scenario):
    events = await run(
        scenario,
        {
            "understood": "You asked about power banks.",
            "language": "en",
            "action": "answer_policy",
            "arguments": {},
            "reply": "Power banks travel in the cabin only, never in checked baggage.",
            "sources": ["https://www.solsticeair.example/help/cabin-baggage"],
        },
    )
    reply = only(events, ReplyReady)[0]
    assert "cabin only" in reply.text
    assert reply.sources == ["https://www.solsticeair.example/help/cabin-baggage"]


async def test_general_answer_carries_no_sources(scenario):
    """Only grounded answers cite. A general-knowledge reply must not look
    like it came from the corpus."""
    events = await run(
        scenario,
        {
            "understood": "You asked how to get into town.",
            "language": "en",
            "action": "answer_general",
            "arguments": {},
            "reply": "The rail link runs from level 1 and takes about half an hour.",
            "sources": ["https://www.solsticeair.example/help/cabin-baggage"],
        },
    )
    assert only(events, ReplyReady)[0].sources == []


async def test_empty_reply_degrades_to_a_refusal_not_a_blank_card(scenario):
    events = await run(
        scenario,
        {
            "understood": "...",
            "language": "en",
            "action": "answer_policy",
            "arguments": {},
            "reply": "   ",
        },
    )
    assert not only(events, ReplyReady)
    assert card(events).card == "refusal"


async def test_unknown_action_degrades_to_a_refusal(scenario):
    events = await run(
        scenario,
        {"understood": "...", "language": "en", "action": "launch_rocket", "arguments": {}},
    )
    assert card(events).card == "refusal"


# -- failure paths ---------------------------------------------------------


class _FailingProvider:
    name = "failing"

    def __init__(self, kind: str) -> None:
        self._kind = kind

    async def stream(self, **_):
        yield Failed(kind=self._kind, detail="simulated")


@pytest.mark.parametrize(
    "kind,expected", [("throttled", "busy"), ("unavailable", "unavailable")]
)
async def test_provider_failure_becomes_a_recoverable_error(scenario, kind, expected):
    engine = Engine(scenario, _FailingProvider(kind))
    events = [e async for e in engine.run("hello")]
    err = only(events, EngineError)[0]
    assert err.kind == kind
    assert err.message
    assert err.actions[0]["id"] == "retry"
    assert only(events, Done)  # the turn still closes cleanly


class _TruncatingProvider:
    name = "truncating"

    async def stream(self, **_):
        yield ArgsFragment('{"understood": "half a sen')


async def test_truncated_stream_does_not_produce_a_card(scenario):
    """A dropped connection must surface as an error, never as a card built
    from a partial decision."""
    engine = Engine(scenario, _TruncatingProvider())
    events = [e async for e in engine.run("hello")]
    assert not only(events, CardReady)
    assert only(events, EngineError)[0].kind == "malformed"
