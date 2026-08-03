"""Scenario packs: the only thing that differs between industries.

A pack is a directory of data. No Python, no imports, nothing the engine has to
special-case:

    scenarios/<id>/
        scenario.yaml       persona, identifier formats, routing rules, tools,
                            card layouts, tool chains
        fixtures.yaml       canned tool responses, per language
        kb/*.md             the small policy corpus that rides in the cached
                            system prefix
        strings/<lang>.yaml the non-tool text the engine writes itself
                            (refusals, errors, button labels, nav targets)

Everything the model is told about a domain is assembled from these files at
load time. `core/` never names an industry.

Adding a language is adding one file to `strings/` plus one column in
`fixtures.yaml`; `tests/test_scenarios.py` asserts that claim rather than
trusting it.
"""

from __future__ import annotations

import functools
import pathlib
import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

SCENARIO_ROOT = pathlib.Path(__file__).resolve().parents[1] / "scenarios"

# Actions the engine understands on top of the pack's own tools. A pack may
# switch the last two off, but it may not invent new ones -- every extra
# behaviour belongs in a tool, so the router's action set stays closed and the
# forced tool schema stays a fixed enum.
BUILTIN_ACTIONS = ("flag_urgent", "refuse_off_topic", "answer_policy", "answer_general")

# Text the engine writes on paths that exist in every pack: the urgent handover,
# the refusal, the two failure states, and the retry button. A pack that omits
# one of these renders the raw key at exactly the moment things are already
# going wrong, so the contract is checked at load rather than left implicit.
ENGINE_REQUIRED_STRINGS = (
    "urgent_message",
    "urgent_primary",
    "urgent_secondary",
    "refusal_message",
    "error_message",
    "busy_message",
    "retry",
)


class ParamSpec(BaseModel):
    """One argument the model may extract for a tool."""

    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = ""
    required: bool = False


class ToolSpec(BaseModel):
    name: str
    description: str
    card: str
    params: dict[str, ParamSpec] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError(f"tool name must be snake_case: {v!r}")
        if v in BUILTIN_ACTIONS:
            raise ValueError(f"tool name collides with a builtin action: {v!r}")
        return v

    def json_schema(self) -> dict:
        """The shape the model is shown for this tool's arguments."""
        return {
            "type": "object",
            "properties": {
                key: {"type": p.type, "description": p.description}
                for key, p in self.params.items()
            },
            "required": [k for k, p in self.params.items() if p.required],
        }


class IdentifierSpec(BaseModel):
    """A domain identifier the model must recognise in speech.

    `pattern` is not used to constrain the model -- it validates what came back,
    so a hallucinated booking reference is dropped before it reaches a tool
    rather than being passed to a live system.
    """

    pattern: str
    description: str
    field: str

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        re.compile(v)
        return v


# The closed vocabulary of shapes a card can take.
#
# This is the constraint that keeps "adding an industry is adding a directory"
# true. The first two packs between them invented nine layout names -- boarding
# pass, appointment, disruption, dispatch, connection, quote, directions,
# detail, notice -- and if the interface hardcoded a component per name, every
# new pack would need frontend work and the claim would quietly be false.
#
# They were never nine shapes. A boarding pass and a booked service visit are
# the same object: a headline entity, a grid of facts, a reference code. A
# flight delay and a dispatched technician are both "something changed, here is
# the new state and what it means for you". Naming them by shape instead of by
# industry collapses nine into six, and a pack now picks from a vocabulary
# rather than inventing one.
#
# A pack that genuinely needs a seventh shape adds it here and in the renderer,
# deliberately and once, rather than by accident on the way past.
PACK_LAYOUTS = (
    "pass",    # a ticket-like artifact: headline entity, field grid, scannable code
    "status",  # something changed: state, before/after, what it means
    "plan",    # a sequence against a deadline, with a risk level
    "range",   # a band between two numbers, with what is included
    "steps",   # an ordered list of instructions
    "detail",  # a prose summary plus supporting fields
)

# Set by the engine, never by a pack: these are the built-in paths.
ENGINE_LAYOUTS = ("urgent", "notice")


class CardSpec(BaseModel):
    """How the interface should render a tool result.

    `layout` picks a shape from the shared vocabulary. `fields` is the contract
    between engine and interface: the generated TypeScript comes from here, so a
    pack that renames a field breaks the build rather than silently rendering an
    empty row.
    """

    layout: str
    fields: list[str] = Field(default_factory=list)
    optional_fields: list[str] = Field(default_factory=list)

    @field_validator("layout")
    @classmethod
    def _known_shape(cls, v: str) -> str:
        if v in ENGINE_LAYOUTS:
            raise ValueError(
                f"layout {v!r} is reserved for the engine's own cards; packs cannot use it"
            )
        if v not in PACK_LAYOUTS:
            raise ValueError(
                f"unknown layout {v!r}. Pick a shape from {list(PACK_LAYOUTS)}, or add a new "
                f"one to PACK_LAYOUTS and implement it in the renderer -- deliberately, "
                f"because every pack then inherits it."
            )
        return v


class ChainStep(BaseModel):
    """One follow-up tool call that fills out a card.

    `args` values are resolved against the originating call. A reference that
    resolves to nothing means the step is skipped -- never substituted with a
    placeholder. That is deliberate: a card missing a section is recoverable, a
    card built from an invented identifier is not.
    """

    tool: str
    args: dict[str, str] = Field(default_factory=dict)


class ChainRule(BaseModel):
    when_tool: str
    when_result: dict[str, Any] = Field(default_factory=dict)
    then: list[ChainStep]

    def matches(self, tool: str, result: dict) -> bool:
        if tool != self.when_tool:
            return False
        return all(result.get(k) == v for k, v in self.when_result.items())


class DemoSpec(BaseModel):
    """A canned utterance with the decision it should produce.

    Replaying one costs no model call and touches no network, which is what
    makes a live demonstration immune to someone else's rate limit. The
    utterances are deliberately written the way stressed people actually
    speak -- fragmented, non-native, mid-panic -- because that is the input the
    routing has to survive, and a demo on tidy sentences proves nothing.
    """

    id: str
    label: str
    utterance: str
    decision: dict[str, Any]


class ScenarioSpec(BaseModel):
    id: str
    name: str
    persona: str
    disambiguation: str = ""
    languages: list[str]
    default_language: str = "en"
    identifiers: dict[str, IdentifierSpec] = Field(default_factory=dict)
    tools: list[ToolSpec]
    cards: dict[str, CardSpec]
    chains: list[ChainRule] = Field(default_factory=list)
    demos: list[DemoSpec] = Field(default_factory=list)
    enable_policy: bool = True
    enable_general: bool = True

    @model_validator(mode="after")
    def _cross_references_resolve(self) -> "ScenarioSpec":
        tool_names = {t.name for t in self.tools}
        if not tool_names:
            raise ValueError(f"scenario {self.id!r} declares no tools")
        for t in self.tools:
            if t.card not in self.cards:
                raise ValueError(f"tool {t.name!r} renders card {t.card!r}, which is not declared")
        for rule in self.chains:
            for name in [rule.when_tool, *(s.tool for s in rule.then)]:
                if name not in tool_names:
                    raise ValueError(f"chain references unknown tool {name!r}")
        seen_demos: set[str] = set()
        for demo in self.demos:
            if demo.id in seen_demos:
                raise ValueError(f"duplicate demo id {demo.id!r}")
            seen_demos.add(demo.id)
            action = demo.decision.get("action")
            if action not in {*tool_names, *BUILTIN_ACTIONS}:
                raise ValueError(f"demo {demo.id!r} routes to unknown action {action!r}")
        if self.default_language not in self.languages:
            raise ValueError(
                f"default_language {self.default_language!r} is not in languages {self.languages}"
            )
        return self

    @property
    def action_names(self) -> list[str]:
        """Every value the router's `action` enum may take."""
        actions = [t.name for t in self.tools] + ["flag_urgent", "refuse_off_topic"]
        if self.enable_policy:
            actions.append("answer_policy")
        if self.enable_general:
            actions.append("answer_general")
        return actions

    def tool(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools if t.name == name), None)

    def demo(self, demo_id: str) -> DemoSpec | None:
        return next((d for d in self.demos if d.id == demo_id), None)


class Scenario:
    """A loaded pack: spec + fixtures + strings + knowledge base."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.spec = ScenarioSpec(**_read_yaml(root / "scenario.yaml"))
        self.fixtures: dict[str, Any] = _read_yaml(root / "fixtures.yaml")
        self.strings: dict[str, dict[str, str]] = {
            lang: _read_yaml(root / "strings" / f"{lang}.yaml")
            for lang in self.spec.languages
        }
        self.knowledge = _read_knowledge(root / "kb")
        self._validate_strings()

    def _validate_strings(self) -> None:
        """Two checks: the pack covers what the engine needs, and every language
        covers what the pack declares.

        Skip the first and a pack renders a bare key on its urgent or error
        path. Skip the second and one language silently falls back to another
        on a subset of the interface.
        """
        base = set(self.strings[self.spec.default_language])

        engine_missing = set(ENGINE_REQUIRED_STRINGS) - base
        if engine_missing:
            raise ValueError(
                f"{self.spec.id}/strings/{self.spec.default_language}.yaml is missing "
                f"keys the engine itself needs: {sorted(engine_missing)}"
            )

        for lang, table in self.strings.items():
            missing = base - set(table)
            if missing:
                raise ValueError(
                    f"{self.spec.id}/strings/{lang}.yaml is missing keys: {sorted(missing)}"
                )

    def text(self, key: str, language: str | None = None) -> str:
        """A piece of engine-authored text in the caller's language.

        Pre-translated at load time, never translated per request: this is what
        keeps a seventh language from costing a seventh round trip.
        """
        table = self.strings.get((language or "")[:2]) or self.strings[self.spec.default_language]
        return table.get(key) or self.strings[self.spec.default_language].get(key, key)

    def fixture(self, tool: str, language: str | None = None) -> dict:
        """The canned result for a tool, localized where the pack provides it."""
        entry = self.fixtures.get(tool)
        if entry is None:
            raise KeyError(f"{self.spec.id} has no fixture for tool {tool!r}")
        base = dict(entry.get("base") or {})
        overlay = (entry.get("localized") or {}).get((language or "")[:2])
        if overlay:
            base.update(overlay)
        return base

    def valid_identifier(self, field: str, value: str) -> bool:
        """Does `value` look like the identifier the pack says it is?

        Anything that fails is dropped rather than forwarded, so a plausible
        but invented reference never reaches a downstream system.
        """
        spec = next((s for s in self.spec.identifiers.values() if s.field == field), None)
        if spec is None:
            return True
        return re.fullmatch(spec.pattern, value.strip()) is not None


def _read_yaml(path: pathlib.Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"scenario pack is missing {path.name}: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _read_knowledge(kb_dir: pathlib.Path) -> str:
    """Concatenate the pack's corpus into one cacheable block.

    Deliberately not a vector store. At this size (single-digit thousands of
    tokens) retrieval costs more than it saves: the whole corpus rides in the
    cached system prefix and is served back at cache-read rates, so the model
    sees all of it on every turn for a fraction of the price of embedding,
    storing, and querying a fraction of it.

    Sorted by filename so the bytes are identical on every process start --
    an unsorted glob would reorder the prefix and silently cost a cache write
    per deploy.
    """
    if not kb_dir.is_dir():
        return ""
    parts = []
    for path in sorted(kb_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if text:
            parts.append(f"=== {path.stem} ===\n{text}")
    return "\n\n".join(parts)


@functools.lru_cache(maxsize=8)
def load_scenario(scenario_id: str) -> Scenario:
    root = SCENARIO_ROOT / scenario_id
    if not root.is_dir():
        available = sorted(p.name for p in SCENARIO_ROOT.iterdir() if p.is_dir())
        raise FileNotFoundError(f"unknown scenario {scenario_id!r}; available: {available}")
    return Scenario(root)


def available_scenarios() -> list[str]:
    if not SCENARIO_ROOT.is_dir():
        return []
    return sorted(p.name for p in SCENARIO_ROOT.iterdir() if (p / "scenario.yaml").is_file())
