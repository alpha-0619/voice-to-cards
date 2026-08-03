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
the decision is still forming. The intent is to leave total time alone and
remove the wait.

> **Not yet measured.** That last sentence is the design's argument, not a
> result. `tools/bench.py` exists to test it and has not been run against a
> live key yet, so [docs/LATENCY.md](docs/LATENCY.md) has no numbers in it.
> Nothing in this repository quotes a latency figure until it does.

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

### Deploying it publicly

**Deploy without `ANTHROPIC_API_KEY`.** With no key configured, `/api/converse`
returns 503 and everything else works: the manifest, and all of the canned
demos, which run the real engine end to end. A public link is then free to
share and impossible to run up a bill on, because there is no key on the box to
spend.

Set a key only where free-form input is actually wanted — a local run, or a
deployment behind auth. The rate limiter exists for the middle case, but the
strongest control is simply not putting a key somewhere the public can reach.

The repository is configured for Vercel: `vercel.json` builds the interface into
`public/`, which is served from the CDN, and `app/main.py` becomes a single
streaming function. Import the repo and deploy — there is nothing to configure,
because the thing you would normally configure is the key, and there isn't one.

To reproduce the deployed shape locally, build once and run the API alone:

```bash
npm --prefix web run build     # → public/
uvicorn app.main:app --port 8000
```

That serves the interface and the API from one process on one port, with no dev
server and no proxy — which is the point of doing it, since "works under the dev
server, breaks in production" is the failure it rules out.

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

## The interface

```bash
cd web && npm install && npm run dev     # http://localhost:5175
```

Vite, React, TypeScript, and no component library. That is the point of the
number below rather than an aesthetic preference: the app this rebuilds pulled
in a UI kit that shipped three icon fonts it never used.

| built output | raw | gzipped |
|---|---:|---:|
| `index.js` | 205.8 KB | 64.7 KB |
| `index.css` | 7.4 KB | 2.3 KB |
| **total (3 files)** | **208.6 KB** | **65.5 KB** |

**Types are generated, not written twice.** `tools/gen_types.py` derives
`web/src/types.gen.ts` from the engine's own dataclasses and the installed
packs, and `tests/test_generated_types.py` fails if the checked-in file has
drifted. Renaming a field on the backend is then a compile error in the
interface, instead of a card that renders with one empty row and is found by a
user.

**Six card shapes, not one per industry.** A pack picks a shape from a closed
vocabulary — `pass`, `status`, `plan`, `range`, `steps`, `detail` — so the two
packs share every renderer. A boarding pass and a booked service visit are the
same object. If a pack could name its own layout, the interface would need a
component per pack and "adding an industry is adding a directory" would quietly
stop being true.

**The card spec is a projection.** Chained tools routinely return more than the
card they are filling can show, and the surplus is dropped before it reaches
the client rather than being silently ignored there. What was dropped is
reported as a trace event, because a field turning up in that list usually
means a card should declare it or a chain is wired to the wrong tool.

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
  scenario.py   pack schema, layout vocabulary, loader
  prompt.py     pack → system blocks + the forced tool
  streaming.py  incremental JSON decoding
  engine.py     routing → validation → tools → projection → card
  memory.py     transcript sanitising
  ratelimit.py  per-IP window + deployment ceiling
  providers/    claude (live) and scripted (offline)
scenarios/  the packs. airport, frontdesk.
app/        FastAPI + SSE
web/        Vite + React. six card shapes, generated types.
tools/      bench.py (latency), gen_types.py (TypeScript)
```

## Handing this over

[NEXT_SESSION.md](NEXT_SESSION.md) is the working handoff: what is verified and
how, what is still an assumption, the gotchas that cost time, and what to do
next. It is written for whoever picks this up, including a future me.

## Note on the airline

Solstice Airways and Kestrel International are invented, as are every
identifier, gate, belt and lounge in the pack. The reference notes cite the
reserved `.example` domain rather than quoting a real carrier.
