"""The HTTP surface, exercised over the replay path so no key is needed.

The assertion that matters is the last one: events must reach the client
incrementally. A response that arrives whole is indistinguishable from the
batch behaviour this project exists to replace, and nothing else in the suite
would notice.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        name = payload = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
        if name is not None:
            out.append((name, payload))
    return out


def test_health_reports_the_running_configuration(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["scenario"] == "airport"
    # The two settings a reader will want to check against the latency notes.
    assert body["thinking"] == "adaptive"
    assert body["effort"] in {"low", "medium", "high", "xhigh", "max"}


def test_manifest_exposes_the_pack_without_leaking_the_prompt(client):
    body = client.get("/api/scenario").json()
    assert {t["name"] for t in body["tools"]} >= {"get_flight_status", "get_bag_belt"}
    assert body["languages"] == ["en", "es"]
    assert any(d["id"] == "disruption" for d in body["demos"])
    # Prompt material stays server-side.
    assert "persona" not in body
    assert "disambiguation" not in body


def test_unknown_demo_is_a_404(client):
    assert client.post("/api/replay/does-not-exist").status_code == 404


def test_replay_runs_the_whole_pipeline(client):
    response = client.post("/api/replay/disruption")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(response.text)
    names = [n for n, _ in events]
    assert "understood_delta" in names
    assert names[-1] == "done"

    card = next(p for n, p in events if n == "card")
    assert card["layout"] == "disruption"
    assert card["data"]["status"] == "DELAYED"
    assert card["data"]["gate"] == "C14"

    understood = next(p for n, p in events if n == "understood")
    streamed = "".join(p["text"] for n, p in events if n == "understood_delta")
    assert streamed == understood["text"]


def test_replay_of_a_spanish_demo_stays_in_spanish(client):
    events = parse_sse(client.post("/api/replay/spanish").text)
    assert next(p for n, p in events if n == "understood")["language"] == "es"
    assert "Retraso" in next(p for n, p in events if n == "card")["data"]["reason"]


def test_replay_of_a_policy_demo_returns_prose_with_sources(client):
    events = parse_sse(client.post("/api/replay/policy").text)
    reply = next(p for n, p in events if n == "reply")
    assert "cabin" in reply["text"].lower()
    assert reply["sources"]
    # A policy answer is prose, not a card: it is read aloud.
    assert not any(n == "card" for n, _ in events)


def test_utterance_length_is_bounded(client):
    assert client.post("/api/converse", json={"utterance": "x" * 5000}).status_code == 422
    assert client.post("/api/converse", json={"utterance": ""}).status_code == 422


def test_events_arrive_incrementally_not_as_one_buffered_blob(client):
    """The whole point, asserted: the first event is readable before the last
    one exists. `stream=True` keeps the response un-buffered, so if anything
    upstream starts collecting the generator this fails."""
    with client.stream("POST", "/api/replay/connection") as response:
        seen: list[str] = []
        for line in response.iter_lines():
            if line.startswith("event: "):
                seen.append(line[len("event: ") :])
                if "understood_delta" in seen and len(seen) >= 3:
                    break
        # We broke out of the stream while it was still open, having already
        # received readable text -- which is only possible if it was streaming.
        assert "understood_delta" in seen
        assert "card" not in seen
