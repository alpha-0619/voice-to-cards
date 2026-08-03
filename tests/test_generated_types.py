"""The interface's types must not drift from the engine's.

This is the whole reason the types are generated instead of written twice. The
failure it prevents is silent: a field gets renamed on the backend, the frontend
keeps reading the old name, a card renders with an empty row, nothing throws.
Here it is a failing test with a one-line fix.
"""

from __future__ import annotations

import subprocess
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_checked_in_types_match_the_engine():
    result = subprocess.run(
        [sys.executable, "tools/gen_types.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, (
        "web/src/types.gen.ts is stale.\n"
        "Run `python tools/gen_types.py` and commit the result.\n\n"
        f"{result.stdout}{result.stderr}"
    )


def test_every_engine_event_is_named_for_the_wire():
    """An event class the SSE layer cannot name is one the interface never
    sees. Both lists are maintained by hand; this is what keeps them equal."""
    from app.main import EVENT_NAMES
    from tools.gen_types import EVENTS

    generated = {cls for _, cls in EVENTS}
    served = set(EVENT_NAMES)
    assert generated == served, (
        f"only generated: {sorted(c.__name__ for c in generated - served)}; "
        f"only served: {sorted(c.__name__ for c in served - generated)}"
    )


def test_wire_names_agree_between_the_two():
    from app.main import EVENT_NAMES
    from tools.gen_types import EVENTS

    for name, cls in EVENTS:
        assert EVENT_NAMES[cls] == name, f"{cls.__name__} is {EVENT_NAMES[cls]!r} on the wire"
