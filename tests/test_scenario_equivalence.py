"""One set of assertions, run across every installed pack.

This is the file that keeps the central claim honest. "The engine is
scenario-agnostic" is easy to say and easy to quietly break -- a pack that only
works because `core/` learned one of its quirks looks fine until the second pack
arrives. So every pack runs the same checks, and a new pack inherits them by
existing.

It is also the pack author's safety net. Most of the ways a pack goes wrong are
silent at runtime: a card promising a field the fixtures never set renders an
empty row, a chain pointing at a tool with no fixture just stops happening. Both
fail loudly here instead.
"""

from __future__ import annotations

import pytest

from core.engine import CardReady, Engine, ReplyReady, Understood
from core.prompt import build_router_tool, build_system_blocks
from core.providers.scripted import ScriptedProvider
from core.scenario import (
    ENGINE_REQUIRED_STRINGS,
    PACK_LAYOUTS,
    available_scenarios,
    load_scenario,
)

PACKS = available_scenarios()

# Parametrising over an empty list would silently skip this entire file, so an
# empty install is a hard error rather than a green run.
assert PACKS, "no scenario packs found under scenarios/"

pytestmark = pytest.mark.parametrize("pack_id", PACKS)


def test_pack_loads(pack_id):
    scenario = load_scenario(pack_id)
    assert scenario.spec.id == pack_id
    assert scenario.spec.tools


def test_every_tool_has_a_fixture_in_every_language(pack_id):
    """A tool the engine can route to but not execute is a dead end that only
    shows up when a caller happens to ask for it."""
    scenario = load_scenario(pack_id)
    for tool in scenario.spec.tools:
        for language in scenario.spec.languages:
            result = scenario.fixture(tool.name, language)
            assert result, f"{pack_id}/{tool.name} has an empty fixture for {language}"


def test_card_fields_are_actually_produced(pack_id):
    """A card promising a field nothing sets renders a blank row forever.

    Required fields must appear in the fixture of at least one tool that renders
    the card -- either directly or via a chain that fills it in.
    """
    scenario = load_scenario(pack_id)
    language = scenario.spec.default_language

    for card_name, card in scenario.spec.cards.items():
        producers = [t for t in scenario.spec.tools if t.card == card_name]
        if not producers:
            continue
        available: set[str] = set()
        for tool in producers:
            available |= set(scenario.fixture(tool.name, language))
            for rule in scenario.spec.chains:
                if rule.when_tool == tool.name:
                    for step in rule.then:
                        try:
                            available |= set(scenario.fixture(step.tool, language))
                        except KeyError:
                            pass
        missing = set(card.fields) - available
        assert not missing, f"{pack_id}/card {card_name} declares unproduced fields: {sorted(missing)}"


def test_localized_overlays_do_not_invent_fields(pack_id):
    """An overlay key that is not in `base` is almost always a typo, and it
    would appear in one language only."""
    scenario = load_scenario(pack_id)
    for tool, entry in scenario.fixtures.items():
        base = set(entry.get("base") or {})
        for language, overlay in (entry.get("localized") or {}).items():
            # A key must exist in `base` or in at least one other language's
            # overlay. A key present in exactly one language is the typo case,
            # and it would show up for that language's callers only.
            others = set(base)
            for other_lang, other in (entry.get("localized") or {}).items():
                if other_lang != language:
                    others |= set(other)
            stray = set(overlay) - others
            assert not stray, f"{pack_id}/{tool}: {language} has keys no other language does: {sorted(stray)}"


def test_cards_use_the_shared_layout_vocabulary(pack_id):
    """A pack picks a shape; it does not invent one.

    Enforced at load, asserted here because it is the constraint that keeps
    "adding an industry is adding a directory" true. The moment a pack can name
    its own layout, the interface needs a component per pack and the claim is
    dead.
    """
    scenario = load_scenario(pack_id)
    for name, card in scenario.spec.cards.items():
        assert card.layout in PACK_LAYOUTS, (
            f"{pack_id}/card {name} uses layout {card.layout!r}, "
            f"which is not one of {list(PACK_LAYOUTS)}"
        )


def test_pack_supplies_the_text_the_engine_itself_writes(pack_id):
    """The engine writes on paths every pack has: the urgent handover, the
    refusal, two failure states, the retry button. A pack missing one of those
    renders a bare key at exactly the moment something has already gone wrong.

    Enforced by the loader; asserted here so it is a named, visible contract
    rather than an unwritten rule the first pack happened to satisfy.
    """
    scenario = load_scenario(pack_id)
    table = scenario.strings[scenario.spec.default_language]
    for key in ENGINE_REQUIRED_STRINGS:
        assert key in table, f"{pack_id} does not supply {key!r}"
        assert table[key].strip(), f"{pack_id} supplies {key!r} but it is empty"


def test_every_language_carries_every_string(pack_id):
    """Enforced by the loader; asserted here so the guarantee is visible and a
    regression names this test rather than blowing up at import time."""
    scenario = load_scenario(pack_id)
    base = set(scenario.strings[scenario.spec.default_language])
    for language, table in scenario.strings.items():
        assert set(table) == base, f"{pack_id}/strings/{language} differs from the default"


def test_engine_text_never_falls_back_silently(pack_id):
    """`text()` returns the key itself when nothing matches. That is a useful
    last resort and a terrible thing to ship, so no declared language may hit it."""
    scenario = load_scenario(pack_id)
    for language in scenario.spec.languages:
        for key in scenario.strings[scenario.spec.default_language]:
            assert scenario.text(key, language) != key, f"{pack_id}/{language} falls back on {key!r}"


def test_prompt_assembles_with_a_single_cache_breakpoint(pack_id):
    """One breakpoint, on the last block, covering tools + instructions + corpus.

    More than one wastes the budget; none means the corpus is re-billed in full
    on every turn.
    """
    scenario = load_scenario(pack_id)
    blocks = build_system_blocks(scenario)
    breakpoints = [b for b in blocks if "cache_control" in b]
    assert len(breakpoints) == 1
    assert breakpoints[0] is blocks[-1]


def test_router_tool_declares_the_restatement_first(pack_id):
    """The streaming contract. If this order changes, perceived latency
    regresses to batch behaviour and nothing else would notice."""
    scenario = load_scenario(pack_id)
    tool = build_router_tool(scenario)
    assert list(tool["input_schema"]["properties"])[0] == "understood"
    assert tool["eager_input_streaming"] is True
    assert set(tool["input_schema"]["required"]) == {"understood", "language", "action"}


def test_identifier_patterns_accept_the_packs_own_examples(pack_id):
    """A pattern that rejects the pack's own demo data is a pattern that will
    reject real callers."""
    scenario = load_scenario(pack_id)
    for demo in scenario.spec.demos:
        for field, value in (demo.decision.get("arguments") or {}).items():
            if isinstance(value, str):
                assert scenario.valid_identifier(field, value), (
                    f"{pack_id}/demo {demo.id}: {field}={value!r} fails the pack's own pattern"
                )


async def test_every_demo_replays_to_the_answer_it_intends(pack_id):
    """End to end, offline: each demo must produce the *specific* answer its
    decision asks for.

    Asserting only that "something rendered" is not enough, and this test used
    to do exactly that. Every degraded path in the engine ends in a refusal
    card, so a demo with a malformed decision still produced a card and still
    passed -- while failing in front of an audience. The assertion has to name
    the expected outcome, not just require one.
    """
    scenario = load_scenario(pack_id)
    for demo in scenario.spec.demos:
        provider = ScriptedProvider(chunk_size=3).queue(demo.decision)
        events = [e async for e in Engine(scenario, provider).run(demo.utterance)]
        where = f"{pack_id}/demo {demo.id}"

        cards = [e for e in events if isinstance(e, CardReady)]
        replies = [e for e in events if isinstance(e, ReplyReady)]
        action = demo.decision.get("action")

        if action in ("answer_policy", "answer_general"):
            assert replies, f"{where}: expected a prose reply, got {[c.card for c in cards]}"
            # Prose answers are read aloud; rendering a card alongside would
            # give the caller two different answers to the same question.
            assert not cards, f"{where}: a prose answer also rendered {[c.card for c in cards]}"
        elif action == "flag_urgent":
            assert cards and cards[0].card == "urgent", f"{where}: expected the urgent card"
        elif action == "refuse_off_topic":
            assert cards and cards[0].card == "refusal", f"{where}: expected the refusal card"
        else:
            tool = scenario.spec.tool(str(action))
            assert tool is not None, f"{where}: routes to unknown action {action!r}"
            assert cards, f"{where}: produced no card"
            assert cards[0].card == tool.card, (
                f"{where}: degraded to {cards[0].card!r} instead of {tool.card!r}"
            )

        understood = [e for e in events if isinstance(e, Understood)]
        assert understood, f"{where}: produced no restatement"
        assert understood[0].language in scenario.spec.languages
