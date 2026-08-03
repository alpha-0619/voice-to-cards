"""HTTP surface: one streaming endpoint, one replay endpoint, one manifest.

Everything interesting happens in `core`. This layer's whole job is to turn the
engine's event stream into Server-Sent Events without buffering it -- buffering
here would quietly undo the entire point of the streaming path, which is why
`/api/converse` yields per event rather than building a response.

Two endpoints, deliberately different:

  POST /api/converse      free-form input, costs a model call, metered
  POST /api/replay/{id}   canned input, costs nothing, never metered

The split is what lets a demonstration survive a spent rate limit.
"""

from __future__ import annotations

import json
import logging
import pathlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.config import get_settings
from core.engine import (
    CardReady,
    Done,
    Engine,
    EngineError,
    ReplyReady,
    Routed,
    Trace,
    Understood,
    UnderstoodDelta,
)
from core.memory import sanitize_history
from core.providers import build_provider
from core.providers.scripted import ScriptedProvider
from core.ratelimit import RateLimiter
from core.scenario import load_scenario

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("v2c")

settings = get_settings()

# Where the built interface lands. Vercel serves this from the CDN; locally the
# routes at the bottom of this file serve it, so both shapes are the same.
PUBLIC_DIR = pathlib.Path(__file__).resolve().parents[1] / "public"

EVENT_NAMES = {
    Trace: "trace",
    UnderstoodDelta: "understood_delta",
    Understood: "understood",
    Routed: "routed",
    CardReady: "card",
    ReplyReady: "reply",
    EngineError: "error",
    Done: "done",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.scenario = load_scenario(settings.scenario)
    # The live engine is built on first use, not at startup: the replay path,
    # the manifest and the health check are all useful without a key, and an
    # app that refuses to boot without one is hostile to anyone cloning it to
    # look around.
    app.state.engine = None
    # Same code path, same decoding, same chaining -- only the source of the
    # fragments differs.
    app.state.replay_engine = Engine(app.state.scenario, ScriptedProvider())
    app.state.limiter = RateLimiter(settings.rate_limit_per_min, settings.rate_limit_daily)
    log.info(
        "serving scenario %r (%d tools, %d demos, languages %s)",
        app.state.scenario.spec.id,
        len(app.state.scenario.spec.tools),
        len(app.state.scenario.spec.demos),
        app.state.scenario.spec.languages,
    )
    yield


app = FastAPI(title="voice-to-cards", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST"],
    allow_headers=["content-type"],
    allow_credentials=False,
)


class ConverseRequest(BaseModel):
    utterance: str = Field(min_length=1, max_length=settings.max_utterance_chars)
    # Held by the client and cleared on reload: nothing about a session is
    # persisted server-side, so there is no transcript here to leak.
    history: list[dict] | None = Field(default=None, max_length=40)


def _sse(event: Any) -> str:
    name = EVENT_NAMES.get(type(event))
    if name is None:
        raise TypeError(f"no SSE name for {type(event).__name__}")
    return f"event: {name}\ndata: {json.dumps(_payload(event), ensure_ascii=False)}\n\n"


def _payload(event: Any) -> dict:
    if isinstance(event, Trace):
        return {"stage": event.stage, "t_ms": event.t_ms, "detail": event.detail}
    if isinstance(event, UnderstoodDelta):
        return {"text": event.text}
    if isinstance(event, Understood):
        return {"text": event.text, "language": event.language}
    if isinstance(event, Routed):
        return {"action": event.action, "arguments": event.arguments}
    if isinstance(event, CardReady):
        return {
            "card": event.card,
            "layout": event.layout,
            "data": event.data,
            "actions": event.actions,
        }
    if isinstance(event, ReplyReady):
        return {"text": event.text, "sources": event.sources}
    if isinstance(event, EngineError):
        return {"kind": event.kind, "message": event.message, "actions": event.actions}
    if isinstance(event, Done):
        return {"usage": event.usage}
    raise TypeError(f"unserialisable event {type(event).__name__}")


async def _stream(engine: Engine, utterance: str, history: list[dict] | None) -> AsyncIterator[str]:
    async for event in engine.run(utterance, history=history):
        yield _sse(event)


def _sse_response(body: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Without this, an nginx in front will buffer the whole stream and
            # the first token arrives with the last one.
            "X-Accel-Buffering": "no",
        },
    )


def _client_key(request: Request) -> str:
    """Who to meter.

    Deliberately the socket peer, not `X-Forwarded-For`: a spoofable header is
    a rate-limit bypass. Behind a trusted proxy this needs the proxy's real
    client header and an allowlist of proxy addresses -- a deployment concern,
    not a default.
    """
    return request.client.host if request.client else "unknown"


def _live_engine() -> Engine:
    if app.state.engine is None:
        # Checked here rather than relying on the client constructor: the SDK
        # accepts a keyless client and only refuses at request time, which
        # would surface as an exception halfway through an open SSE stream
        # instead of an honest 503 before one is opened.
        if not settings.anthropic_api_key:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Free-form input needs ANTHROPIC_API_KEY. This deployment does not "
                    "have one, which is the recommended posture for a public link: the "
                    "canned demos under /api/replay/{id} run the same engine end to end "
                    "and cost nothing."
                ),
            )
        try:
            app.state.engine = Engine(app.state.scenario, build_provider("claude"))
        except Exception as exc:  # missing key, missing SDK, bad config
            raise HTTPException(
                status_code=503,
                detail=(
                    "Free-form input needs ANTHROPIC_API_KEY. This deployment does not "
                    "have one, which is the recommended posture for a public link: the "
                    "canned demos under /api/replay/{id} run the same engine end to end "
                    "and cost nothing. "
                    f"({type(exc).__name__})"
                ),
            ) from exc
    return app.state.engine


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "scenario": app.state.scenario.spec.id,
        "model": settings.model,
        "effort": settings.effort,
        "thinking": settings.thinking,
        "streaming": settings.streaming,
        "has_key": bool(settings.anthropic_api_key),
        "limits": app.state.limiter.snapshot(),
    }


@app.get("/api/scenario")
async def scenario_manifest() -> dict:
    """Everything the frontend needs to render this pack, and nothing more.

    The persona, the corpus and the routing rules stay server-side; they are
    prompt material, not UI material.
    """
    scenario = app.state.scenario
    spec = scenario.spec
    return {
        "id": spec.id,
        "name": spec.name,
        "languages": spec.languages,
        "default_language": spec.default_language,
        "tools": [{"name": t.name, "description": t.description, "card": t.card} for t in spec.tools],
        "cards": {
            name: {
                "layout": c.layout,
                "fields": c.fields,
                "optional_fields": c.optional_fields,
            }
            for name, c in spec.cards.items()
        },
        "demos": [{"id": d.id, "label": d.label, "utterance": d.utterance} for d in spec.demos],
    }


@app.post("/api/converse")
async def converse(req: ConverseRequest, request: Request):
    verdict = app.state.limiter.check(_client_key(request))
    if not verdict.allowed:
        raise HTTPException(
            status_code=429,
            detail=verdict.reason,
            headers={"Retry-After": str(verdict.retry_after_s)},
        )

    engine = _live_engine()
    history = sanitize_history(req.history, max_turns=settings.max_history_turns)
    log.info("converse %r (history=%d turns)", req.utterance[:120], len(history) // 2)
    return _sse_response(_stream(engine, req.utterance, history))


@app.get("/")
async def index():
    """Serve the built interface, when there is one.

    On Vercel this route is usually shadowed: `public/**` is served from the
    CDN, so the request never reaches the function. It matters locally, where
    it means `uvicorn app.main:app` alone serves the whole thing on one port
    and reproduces the deployed shape -- worth having, because "works under the
    dev server, breaks in production" is the failure this avoids.
    """
    index_file = PUBLIC_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(
            status_code=404,
            detail="No built interface. Run `npm --prefix web run build`, or use the dev server.",
        )
    return FileResponse(index_file)


@app.post("/api/replay/{demo_id}")
async def replay(demo_id: str):
    """Replay a canned utterance with no model call and no metering.

    Same engine, same decoding, same chaining -- the only thing swapped is
    where the fragments come from. That equivalence is the point: what an
    audience sees is the real pipeline, not a mock of it.
    """
    demo = app.state.scenario.spec.demo(demo_id)
    if demo is None:
        raise HTTPException(status_code=404, detail="unknown demo")

    engine: Engine = app.state.replay_engine
    engine.provider.queue(demo.decision)
    return _sse_response(_stream(engine, demo.utterance, None))


# Mounted last so it cannot shadow an API route. Only present when a build
# exists, so a source checkout without one still starts and still serves the API.
if (PUBLIC_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=PUBLIC_DIR / "assets"), name="assets")
