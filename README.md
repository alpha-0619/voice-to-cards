# voice-to-cards

One spoken sentence in. One structured card out. Streamed, so the first words
are readable long before the answer is finished.

Most assistants answer with a paragraph. This one answers with a **card** — a
typed payload the UI renders as a boarding pass, a delay notice with a gate and
a voucher, a rescue plan for a connection you are about to miss. The model does
not write the answer; it decides which answer, in one call, and the engine
assembles it.

The engine knows nothing about any industry. Everything domain-specific — the
tools, the identifier formats, the routing rules, the follow-up chains, the
sentences the engine writes itself — lives in a **scenario pack**, which is a
directory of YAML and Markdown. Adding an industry is adding a directory.

```
utterance
  → one forced tool call carrying the whole routing decision
  → decoded while it is still being written
  → identifiers validated against the pack's own patterns
  → tool call + follow-up chain
  → a typed card
```

## Why it is built this way

**One forced call, not a multi-step agent.** The model is asked for exactly one
tool call whose arguments *are* the decision: the restatement, the language, the
action, the arguments. There is no free text to fail to parse and no second round
trip. It is a shallower design than an agent loop, on purpose — routing one
utterance is a shallow problem, and the depth costs seconds a caller can feel.

**The first field is the one a person wants.** The tool schema declares
`understood` first, so it is written first. The engine walks the argument JSON
as it arrives and emits that sentence character by character while the rest of
the decision is still forming. Same total time; almost none of the wait.

**A small corpus rides in the cache instead of a vector store.** The reference
notes are a few thousand tokens. At that size, retrieval costs more than it
saves: the whole corpus sits in the cached system prefix, the model sees all of
it every turn, and after the first request it is billed at cache-read rates. No
index, no embeddings, no infrastructure.

**A missing identifier is left missing.** If a follow-up step needs a booking
reference the caller never gave, the step is skipped and the card comes back
with one section fewer. It is never filled from an earlier turn or a default.
A card missing a section is recoverable; a card built from the wrong person's
reference is not, and it looks completely normal.

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env          # add ANTHROPIC_API_KEY for the live path
pytest -q                     # the whole suite runs without a key
uvicorn app.main:app --reload
```

```bash
curl -N localhost:8000/api/replay/connection     # canned, no model call
curl -N localhost:8000/api/converse -H 'content-type: application/json' \
     -d '{"utterance":"is SX412 delayed? booking SX7K2Q4"}'
```

| endpoint | input | costs a model call | metered |
|---|---|---|---|
| `POST /api/converse` | free-form | yes | yes |
| `POST /api/replay/{id}` | canned | no | no |
| `GET /api/scenario` | — | no | no |

The split is deliberate: a demonstration runs through the identical pipeline
with no request leaving the box, so it cannot fail because of someone else's
rate limit, and it keeps working after the day's ceiling is spent.

## A scenario pack

```
scenarios/airport/
  scenario.yaml       persona, identifier formats, routing rules, tools,
                      card layouts, follow-up chains, canned demos
  fixtures.yaml       tool results, with per-language overlays
  kb/*.md             the corpus that rides in the cached prefix
  strings/en.yaml     refusals, errors, buttons, place names
  strings/es.yaml
```

No Python. `core/` never names an industry, and
`tests/test_scenario_equivalence.py` runs one set of assertions across every
installed pack rather than trusting that claim.

Adding a language is adding one file to `strings/` and one overlay column in
`fixtures.yaml`. Nothing is translated at request time, which is why the
seventh language costs the same as the first.

## Tests

```bash
pytest -q
```

Written to fail rather than to pass. The cases are the ones where doing the
obvious thing produces a confident wrong answer: a fragment boundary landing
inside a `\uXXXX` escape, a surrogate pair split across two chunks, a `}`
inside caller text, a truncated stream, an invented booking reference, a
follow-up step whose argument does not exist.

The suite is checked against deliberate mutations — each guard is removed in
turn and a named test has to notice. It currently catches 6 of 6.

## Measuring it

```bash
python tools/bench.py --runs 7 --json bench_results/baseline.json
```

Reads the engine's own trace events, reports cold and warm runs separately, and
prints a table you can paste. See [docs/LATENCY.md](docs/LATENCY.md) for what
the numbers mean and which design decisions they drove.

## Layout

```
core/       the engine. scenario-agnostic.
  scenario.py   pack schema and loader
  prompt.py     pack → system blocks + the forced tool
  streaming.py  incremental JSON decoding
  engine.py     routing → validation → tools → card
  memory.py     transcript sanitising
  ratelimit.py  per-IP window + deployment ceiling
  providers/    claude (live) and scripted (offline)
scenarios/  the packs
app/        FastAPI + SSE
tools/      the latency harness
```

## Note on the airline

Solstice Airways and Kestrel International are invented, as are every
identifier, gate, belt and lounge in the pack. The reference notes cite the
reserved `.example` domain rather than quoting a real carrier.
