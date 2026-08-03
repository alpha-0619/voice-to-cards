"""History sanitising and the request guards.

Both are cheap to get subtly wrong in ways that only show up in production: a
transcript the API rejects, or a counter that never resets.
"""

from __future__ import annotations

from core.memory import MAX_CONTENT_CHARS, sanitize_history
from core.ratelimit import RateLimiter


# -- history ---------------------------------------------------------------


def test_well_formed_history_survives_intact():
    history = [
        {"role": "user", "content": "Is SX412 delayed?"},
        {"role": "assistant", "content": "You asked about flight SX412."},
    ]
    assert sanitize_history(history) == history


def test_transcript_always_alternates_starts_user_ends_assistant():
    """The API rejects anything else, so this is a hard shape, not a nicety."""
    messy = [
        {"role": "assistant", "content": "orphan opener"},
        {"role": "user", "content": "a"},
        {"role": "user", "content": "duplicate role"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "trailing user"},
    ]
    out = sanitize_history(messy)
    assert out[0]["role"] == "user"
    assert out[-1]["role"] == "assistant"
    assert all(out[i]["role"] != out[i + 1]["role"] for i in range(len(out) - 1))


def test_turn_cap_keeps_the_most_recent_exchanges():
    history = []
    for i in range(20):
        history.append({"role": "user", "content": f"u{i}"})
        history.append({"role": "assistant", "content": f"a{i}"})
    out = sanitize_history(history, max_turns=3)
    assert len(out) == 6
    assert out[0]["content"] == "u17"


def test_long_turns_are_truncated_not_dropped():
    out = sanitize_history(
        [
            {"role": "user", "content": "x" * 5000},
            {"role": "assistant", "content": "ok"},
        ]
    )
    assert len(out[0]["content"]) == MAX_CONTENT_CHARS


def test_junk_is_discarded_rather_than_repaired():
    """Inventing a turn to patch a malformed transcript would put words in the
    caller's mouth, so entries that do not fit are dropped."""
    assert sanitize_history("not a list") == []
    assert sanitize_history([None, 42, "text"]) == []
    assert sanitize_history([{"role": "system", "content": "hi"}]) == []
    assert sanitize_history([{"role": "user", "content": ""}]) == []
    assert sanitize_history([{"role": "user", "content": {"nested": "object"}}]) == []


# -- rate limiting ---------------------------------------------------------


def test_per_minute_window_admits_exactly_the_limit():
    limiter = RateLimiter(per_minute=3, daily_cap=1000)
    now = 1_000_000.0
    assert [limiter.check("ip", now=now).allowed for _ in range(4)] == [True, True, True, False]


def test_window_slides_rather_than_resetting_on_the_minute():
    limiter = RateLimiter(per_minute=2, daily_cap=1000)
    now = 1_000_000.0
    limiter.check("ip", now=now)
    limiter.check("ip", now=now + 30)
    assert not limiter.check("ip", now=now + 40).allowed
    # The first call ages out 60s after it happened, not at a wall-clock tick.
    assert limiter.check("ip", now=now + 61).allowed


def test_clients_are_metered_independently():
    limiter = RateLimiter(per_minute=1, daily_cap=1000)
    now = 1_000_000.0
    assert limiter.check("a", now=now).allowed
    assert not limiter.check("a", now=now).allowed
    assert limiter.check("b", now=now).allowed


def test_daily_cap_is_deployment_wide_and_rolls_over():
    limiter = RateLimiter(per_minute=1000, daily_cap=2)
    now = 1_000_000.0
    assert limiter.check("a", now=now).allowed
    assert limiter.check("b", now=now).allowed
    blocked = limiter.check("c", now=now)
    assert not blocked.allowed and blocked.reason == "daily_cap"
    assert blocked.retry_after_s > 0
    assert limiter.check("c", now=now + 86_400).allowed


def test_retry_after_is_usable_by_a_client():
    limiter = RateLimiter(per_minute=1, daily_cap=1000)
    now = 1_000_000.0
    limiter.check("ip", now=now)
    verdict = limiter.check("ip", now=now + 10)
    assert verdict.reason == "per_minute"
    assert 1 <= verdict.retry_after_s <= 60
