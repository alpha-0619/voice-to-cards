"""Measure the thing the design claims, and print it in a form you can paste.

Three numbers per run, taken from the engine's own trace events so the harness
measures the same clock the app does:

  first_text   the restatement becomes readable  <- what a person experiences
  decision     the routing is fully decoded      <- what the app can act on
  card         the card is assembled             <- the old "response time"

The comparison that matters is `first_text` under streaming against `card`
without it. Those are the two latencies a user can actually tell apart; total
time barely moves, which is the point and also why "make the model faster" was
never going to work.

Two rules this harness follows so the numbers stay honest:

  * The first run of every configuration writes the prompt cache. It is
    reported separately, never folded into a median. A cold write silently
    averaged into warm runs is the most common way a benchmark flatters itself.
  * Every configuration runs the same utterances in the same order.

Usage
-----
    python tools/bench.py                          # streaming vs not, warm
    python tools/bench.py --runs 7
    python tools/bench.py --sweep-effort low,medium,high
    python tools/bench.py --sweep-model claude-opus-5,claude-sonnet-5
    python tools/bench.py --json bench_results/run.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.config import get_settings  # noqa: E402
from core.engine import CardReady, Done, Engine, EngineError, ReplyReady, Trace  # noqa: E402
from core.providers.claude import ClaudeProvider  # noqa: E402
from core.scenario import load_scenario  # noqa: E402


@dataclass
class Run:
    utterance: str
    ok: bool
    first_text_ms: int | None = None
    decision_ms: int | None = None
    card_ms: int | None = None
    total_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    action: str = ""
    error: str = ""


@dataclass
class Configuration:
    label: str
    model: str
    effort: str
    streaming: bool
    cold: Run | None = None
    warm: list[Run] = field(default_factory=list)


async def one_run(engine: Engine, utterance: str) -> Run:
    started = time.monotonic()
    run = Run(utterance=utterance, ok=False)
    try:
        async for event in engine.run(utterance):
            if isinstance(event, Trace):
                if event.stage == "first_text" and run.first_text_ms is None:
                    run.first_text_ms = event.t_ms
                elif event.stage == "routed":
                    run.decision_ms = event.t_ms
                    run.action = str(event.detail.get("action", ""))
            elif isinstance(event, (CardReady, ReplyReady)) and run.card_ms is None:
                run.card_ms = int((time.monotonic() - started) * 1000)
            elif isinstance(event, EngineError):
                run.error = f"{event.kind}: {event.message[:80]}"
            elif isinstance(event, Done):
                run.input_tokens = event.usage.get("input_tokens", 0)
                run.output_tokens = event.usage.get("output_tokens", 0)
                run.cache_read = event.usage.get("cache_read", 0)
                run.cache_write = event.usage.get("cache_write", 0)
    except Exception as exc:  # a harness must report a failure, not hide it
        run.error = f"{type(exc).__name__}: {exc}"

    run.total_ms = int((time.monotonic() - started) * 1000)
    run.ok = not run.error and run.card_ms is not None
    return run


async def run_configuration(label: str, utterances: list[str], repeats: int) -> Configuration:
    settings = get_settings()
    scenario = load_scenario(settings.scenario)
    engine = Engine(scenario, ClaudeProvider())
    config = Configuration(
        label=label, model=settings.model, effort=settings.effort, streaming=settings.streaming
    )

    # Cold: pays the cache write. Reported, never averaged in.
    config.cold = await one_run(engine, utterances[0])
    print(f"  cold  {_line(config.cold)}", flush=True)

    for i in range(repeats):
        utterance = utterances[i % len(utterances)]
        run = await one_run(engine, utterance)
        config.warm.append(run)
        print(f"  warm  {_line(run)}", flush=True)

    return config


def _line(run: Run) -> str:
    if run.error:
        return f"FAILED  {run.error}"
    return (
        f"first_text={_fmt(run.first_text_ms):>6}  decision={_fmt(run.decision_ms):>6}  "
        f"card={_fmt(run.card_ms):>6}  cache_read={run.cache_read:>6}  "
        f"cache_write={run.cache_write:>6}  action={run.action}"
    )


def _fmt(value: int | None) -> str:
    return "-" if value is None else f"{value}ms"


def _stat(values: list[int | None]) -> tuple[str, str]:
    present = [v for v in values if v is not None]
    if not present:
        return "-", "-"
    median = int(statistics.median(present))
    worst = max(present)
    return f"{median}ms", f"{worst}ms"


def report(configs: list[Configuration]) -> str:
    lines: list[str] = []
    lines.append("| configuration | first text (med / worst) | decision | card | cache read |")
    lines.append("|---|---|---|---|---|")
    for c in configs:
        ok = [r for r in c.warm if r.ok]
        if not ok:
            lines.append(f"| {c.label} | no successful runs | - | - | - |")
            continue
        ft_med, ft_max = _stat([r.first_text_ms for r in ok])
        de_med, _ = _stat([r.decision_ms for r in ok])
        cd_med, _ = _stat([r.card_ms for r in ok])
        cache = int(statistics.median([r.cache_read for r in ok]))
        lines.append(f"| {c.label} | {ft_med} / {ft_max} | {de_med} | {cd_med} | {cache:,} |")

    lines.append("")
    lines.append("Cold runs (first request of each configuration, cache being written):")
    for c in configs:
        if c.cold:
            lines.append(f"  {c.label}: {_line(c.cold)}")

    streaming = next((c for c in configs if c.streaming), None)
    batched = next((c for c in configs if not c.streaming), None)
    if streaming and batched:
        s_ok = [r for r in streaming.warm if r.ok]
        b_ok = [r for r in batched.warm if r.ok]
        if s_ok and b_ok:
            s_first = statistics.median([r.first_text_ms for r in s_ok if r.first_text_ms])
            b_card = statistics.median([r.card_ms for r in b_ok if r.card_ms])
            s_card = statistics.median([r.card_ms for r in s_ok if r.card_ms])
            lines.append("")
            lines.append(
                f"Perceived wait: {int(s_first)}ms streaming vs {int(b_card)}ms batched "
                f"({b_card / max(s_first, 1):.1f}x)."
            )
            lines.append(
                f"Total time to the finished card barely moved: {int(s_card)}ms vs "
                f"{int(b_card)}ms. Streaming did not make the model faster -- it removed "
                f"the wait, which is the part a person can feel."
            )
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5, help="warm runs per configuration")
    parser.add_argument("--sweep-effort", default="", help="comma-separated effort levels")
    parser.add_argument("--sweep-model", default="", help="comma-separated model ids")
    parser.add_argument("--json", default="", help="also write raw results here")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set; this harness measures live calls.", file=sys.stderr)
        return 2

    scenario = load_scenario(settings.scenario)
    utterances = [d.utterance for d in scenario.spec.demos] or ["Is my flight delayed?"]

    configs: list[Configuration] = []

    if args.sweep_effort or args.sweep_model:
        efforts = [e.strip() for e in args.sweep_effort.split(",") if e.strip()] or [settings.effort]
        models = [m.strip() for m in args.sweep_model.split(",") if m.strip()] or [settings.model]
        for model in models:
            for effort in efforts:
                settings.model, settings.effort, settings.streaming = model, effort, True
                label = f"{model} effort={effort}"
                print(f"\n{label}", flush=True)
                configs.append(await run_configuration(label, utterances, args.runs))
    else:
        for streaming in (True, False):
            settings.streaming = streaming
            label = f"{settings.model} {'streaming' if streaming else 'batched'}"
            print(f"\n{label}", flush=True)
            configs.append(await run_configuration(label, utterances, args.runs))

    print("\n" + report(configs))

    if args.json:
        path = pathlib.Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([asdict(c) for c in configs], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nraw results -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
