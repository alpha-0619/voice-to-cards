# Latency notes

> **Status: not measured yet.** This file has no numbers in it, and that is
> deliberate rather than an oversight. The design below is an argument about
> where the time goes; until `tools/bench.py` has run against a live key it
> stays an argument. Nothing here or in the README quotes a figure in the
> meantime.

Measurements go here once the harness has been run. Nothing in this file will
be estimated: every number will be produced by the harness, which reads the
engine's own trace events, and every table will state the model, the effort
level and the run count it came from.

Run it with:

```bash
python tools/bench.py --runs 7 --json bench_results/baseline.json
```

## What is measured

| metric | meaning |
|---|---|
| `first_text` | the restatement becomes readable — what a person experiences as "it responded" |
| `decision` | the routing is fully decoded — what the app can act on |
| `card` | the card is assembled — the number a batched implementation would call "response time" |

The comparison worth making is `first_text` under streaming against `card`
without it. Total time barely moves either way, which is the finding: the win
comes from removing the wait, not from making the model faster.

## Two rules the harness follows

1. **Cold runs are reported, never averaged.** The first request of every
   configuration writes the prompt cache. Folding that into a median is the
   most common way a benchmark flatters itself.
2. **Every configuration runs the same utterances in the same order**, taken
   from the scenario pack's `demos`, so a model sweep is not also a workload
   change.

## Decisions this measurement drove

### Thinking stays on, at low effort

The obvious latency move is `thinking: {type: "disabled"}`. It is a trap on
this model: with thinking off, a forced tool call can come back written into
the visible response text instead of as a tool call. The request returns 200,
nothing raises, and the tool simply never runs. In an agentic loop the phantom
text then pollutes every later turn.

Low effort gets the same token savings without opening that door, so the
engine runs `thinking: adaptive` with `effort: low` and the config comments
say why.

### No sampling parameters

`temperature`, `top_p` and `top_k` are rejected outright on current models.
Determinism comes from the forced tool call and a closed action enum, not from
a temperature of zero — which never guaranteed identical output anyway.

### Retries off

The SDK retries a throttle with tens of seconds of backoff. On screen that is
indistinguishable from a hang. Failing immediately lets the app show a
recoverable state, which is the better outcome even though it looks worse in a
success-rate metric.

## Cache effectiveness

`cache_read` is reported on every run. It is the number to watch, not the wall
clock: if it stays at zero across identical-prefix requests, something is
quietly changing the prefix — a timestamp, a per-caller string, an unsorted
serialisation — and the corpus is being re-billed in full every turn.

The prompt is assembled once at startup and never rebuilt per request, and the
corpus files are read in sorted order, precisely so those bytes are stable.
