# Handoff

**Read this first. It is the current state of truth for this project.**

Status: deployed and verified. One 30-second step is still waiting on the user
(CI). Five things the design rests on have never been measured against a live
model, and the repository says so everywhere rather than implying otherwise.

Live: <https://voice-to-cards.vercel.app> (public, no login, no key on the box)
Repo: <https://github.com/alpha-0619/voice-to-cards> (public, `main`)
Local: `C:\Users\User\projects\voice-to-cards`

The other two Vercel hostnames for this project sit behind the team's SSO and
are useless as a portfolio link. Share the one above.

---

## What this is, and why it exists

A rewrite of a delivered client project, from scratch, as an asset the user
owns. The original cannot be published: Upwork's default terms put the work
product with the client on full payment, and its branding is threaded through
42 files. So none of it was copied. The architecture ideas came across; every
line is new.

One spoken sentence in, one structured card out, streamed. The engine is
scenario-agnostic: everything domain-specific lives in a directory of YAML and
Markdown, and adding an industry adds a directory.

Three things it is meant to do for the user:

1. **Portfolio.** Public, readable, with the engineering reasoning in the
   commit messages and the module docstrings rather than in a covering note.
2. **Article material.** Several findings are reusable and counter-intuitive.
   See "What is worth writing about" below.
3. **Product prototype.** The second scenario pack is an HVAC phone front desk,
   which is the shape of the AI receptionist service the user sells.

---

## Do this next

### Measure the five assumptions (needs a key; the user has declined so far)

`tools/bench.py` is written and unused. Everything below is currently an
argument, not a result:

| Assumption | Why it matters | If wrong |
|---|---|---|
| The model writes `understood` first, because the tool schema declares it first | The entire streaming design rests on this one property | Both packs and the renderer need rework |
| `eager_input_streaming: true` produces incremental `input_json_delta` | Without it there is nothing to decode incrementally | Falls back to batch; the perceived-latency claim dies |
| `thinking: adaptive` works with a forced `tool_choice` | The provider sets both | Config change, probably small |
| The prompt cache actually hits | Cost, not correctness | Cost only |
| Actual latency numbers | The headline claim | The claim changes |

Run it with a key in `.env` (gitignored, verified not to leak into git, logs, or
bench output):

```bash
python tools/bench.py --runs 7 --json bench_results/baseline.json
```

It reports cold and warm runs separately and prints a pasteable table. Roughly
24 short calls; cost is negligible.

**The user has been asked twice and chosen not to supply a key.** Do not push on
it. Do not write a latency number anywhere until it has been measured, and do
not let the phrasing drift into implying one. `docs/LATENCY.md` opens with a
"not measured yet" banner; keep it until it is false.

---

## What is verified, and how

| Claim | Evidence |
|---|---|
| 106 tests pass without an API key | `pytest -q`, and `tests/conftest.py` forces an empty key so no test can reach the network |
| The suite has teeth | Mutation-checked: every guard removed in turn, a named test catches each. 6/6 on the core, plus targeted checks on the pack tests and the merge order |
| Adding an industry does not touch the engine | `git diff --stat 10a8cda~1 10a8cda -- core/ app/ tests/` is empty. That commit adds a whole second industry |
| Both packs render through one interface | Driven in a real browser: every demo, every layout, both languages, zero frontend changes between packs |
| The deployed shape works | Built into `public/`, served by `uvicorn app.main:app` alone on one port, no dev server and no proxy, all seven demos verified through it |
| No secrets or client references in the repo | `git grep` for client names, the original project, and secret-shaped strings: all clean before the push |
| The public deployment works | On <https://voice-to-cards.vercel.app>: `/health` reports `has_key: false`; all seven demos return 200 with a complete event stream; six end in a `card`, `policy` ends in a `reply` because that is how the scenario defines it; `index.html` and both assets are served by the CDN; the interface renders both languages |

---

## What is worth writing about

Each of these is reusable, and each was found rather than designed:

1. **Turning thinking off is a trap on this model.** With `thinking: disabled`,
   a forced tool call can come back written into the visible response text
   instead of as a tool call. The request returns 200, nothing raises, and the
   tool never runs. Low effort gets the same savings without that. The provider
   runs `adaptive` at `low` effort for exactly this reason and the file says so.
2. **A small corpus does not want a vector store.** A few thousand tokens ride
   in the cached system prefix and are served at cache-read rates. No index, no
   embeddings, no infrastructure, and the model sees all of it every turn.
3. **The first field is the whole design.** Declaring the human-readable
   restatement first in the tool schema is what puts readable text on screen
   while the decision is still forming. Same total time, almost none of the wait.
4. **Layout names were the hidden coupling.** Two packs invented nine layout
   names; they were six shapes. A boarding pass and a booked service visit are
   the same object. Had the interface hardcoded a component per name, "adding an
   industry is adding a directory" would have quietly become false.
5. **Five bugs the server-side suite could not catch**, all found by looking at
   the actual page or by deliberately breaking things. They are listed in the
   commit messages of `3158e11` and `e043a8a` with the reasoning.

---

## Gotchas that cost time

**Vercel's FastAPI preset ignores what the build command writes.** It collects
static assets from the source tree. The first deployment ran `buildCommand`
perfectly -- the log shows all three files written into `public/` -- and served
none of them: the Resources tab said **0 static assets**, and every request fell
through to the function, which answered its own 404. Setting `outputDirectory`
to `public` and redeploying without cache changed nothing, still 0. Committing
`public/` gives 3. That is why `public/` is in the repository and why
`buildCommand` is gone; changing anything under `web/` now needs a rebuild and a
commit. Verified with every project-settings override cleared, so a fresh import
of this repo behaves the same way.

**The canned demos cannot demonstrate streaming, and never could.**
`ScriptedProvider` has no pacing: it emits the whole payload in a tight loop, in
about a millisecond. Measured client-side, the full 4,896-byte body of a replay
arrives with a **0 ms span** between the first read and the last, locally *and*
on Vercel. There is no gap for anything to stream into. The interface says so
itself -- it prints "first readable text at 261 ms · full answer at 261 ms
(canned, no model call)". So do not treat "the demo did not visibly stream" as a
deployment fault, and do not use the canned path to claim streaming works. That
still needs a key, and it is assumption 2 in the table above.

**`pytest` and `python -m pytest` do not agree here, and CI runs the one that
fails.** `python -m pytest` puts the working directory on `sys.path`; the bare
`pytest` command does not. `tools` is deliberately not part of the installed
package, so under a clean `pip install -e ".[dev]"` the bare command could not
import it and `tests/test_generated_types.py` failed on both Python versions
while `python -m pytest` reported 106 passed. `pythonpath = ["."]` in
`pyproject.toml` now makes both forms agree. The general lesson is the reason
this is written down: a suite verified with a different command than CI runs has
not been verified.

**The `workflow` scope gates the REST API too, and lies about it.** Creating
`.github/workflows/ci.yml` through `PUT /repos/{owner}/{repo}/contents/...`
returns **404**, not 403, while a `GET` on `docs/` in the same branch with the
same token succeeds. So the missing scope reads as "file not found". The web
editor is the only route that works without re-authenticating.

**gh scopes.** `gh auth status` from a Git Bash shell
reports logged in as `alpha-0619` from the Windows keyring, with scopes
`gist, read:org, repo`. The same `gh auth refresh -s workflow` run from the
user's cmd session reports "not logged in to any hosts". `hosts.yml` holds no
token, so the credential is keyring-only and the two shells resolve it
differently. Not diagnosed further because it does not block anything: the push
works, only workflow files are gated. Do not send the user round the
`gh auth login` loop again — an interrupted attempt already cost a round trip.

**Ports.** 5173/5174 and 8000/8001 belong to other projects on this machine.
This one uses **8010** (API) and **5175** (Vite). Both are registered in
`C:\Users\User\.claude\launch.json` as `v2c-api` and `v2c-web`.

**The API does not hot-reload.** Started without `--reload`. After changing
anything under `core/` or `app/`, stop and start `v2c-api` or the change is not
live, and the browser will show stale behaviour with no error.

**`.env` is read from the package directory, not the working directory.** Fixed
after it silently served the wrong scenario. If a setting seems ignored, check
which `.env` is actually being read.

**The Bash tool's working directory persists between calls.** A `rm` ran against
the wrong path once because of it. Prefer absolute paths.

**Screenshots need the Browser pane displayed.** They time out otherwise. DOM
inspection through `javascript_tool` is more precise anyway and was used for all
the UI verification here.

---

## How to run it

```bash
cd C:\Users\User\projects\voice-to-cards

pytest -q                                  # 106 tests, no key needed
python tools/gen_types.py                  # regenerate web/src/types.gen.ts
python tools/gen_types.py --check          # what CI would assert

npm --prefix web run build                 # → public/, then commit it
uvicorn app.main:app --port 8010           # serves the built app and the API
```

Nothing is running. Both dev servers were stopped at the end of the last
session, and ports 8010 and 5175 are free. Start them with `preview_start`
using the names `v2c-api` and `v2c-web` from `C:\Users\User\.claude\launch.json`.
The Vite one proxies `/api` to 8010, so start the API first.

To serve the other pack, put `SCENARIO=frontdesk` in `.env` and restart the API.

---

## Shape of the thing

```
core/       the engine. never names an industry.
  scenario.py   pack schema, the six-shape layout vocabulary, loader
  prompt.py     pack → system blocks + the forced router tool
  streaming.py  incremental JSON decoding, the interesting file
  engine.py     routing → validation → tools → chains → projection → card
  memory.py     transcript sanitising
  ratelimit.py  per-IP window + deployment ceiling
  providers/    claude (live) and scripted (offline, zero network)
scenarios/  airport, frontdesk. YAML and Markdown only.
app/        FastAPI + SSE. also serves public/ when built.
web/        Vite + React. 208.6 KB raw / 65.5 KB gzipped, three files.
tools/      bench.py (unused, needs a key), gen_types.py
```

## Decisions already made — do not relitigate without a reason

- **No key in the public deployment.** The user's constraint, and the
  architecture answers it: canned demos carry the whole demonstration.
- **Fictional brands throughout.** Solstice Airways, Kestrel International,
  Ridgeline Heating & Air. Reference notes cite the reserved `.example` domain.
  Nothing here quotes a real company.
- **Not copied from the original.** Ideas yes, code no. Keep it that way.
- **`public/` is committed.** Not a convenience; the platform gives no choice.
  See the first gotcha. Do not "clean this up" by restoring `buildCommand`.
- **Six card shapes, closed vocabulary.** A pack picks one. Adding a seventh is
  deliberate and touches the renderer; that friction is the point.
- **Latency stays unquoted until measured.**
