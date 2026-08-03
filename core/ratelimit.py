"""Two guards, sized for a public demo rather than a production tenant.

A per-IP sliding window stops one client hammering the model. A whole-deployment
daily ceiling bounds the bill if the link gets shared somewhere busy. Neither
protects against a determined attacker with a pool of addresses -- that is not
what they are for. They exist so a link can be handed out without the worst case
being a surprise invoice.

The design decision worth stating: the canned scenario path deliberately does
not consult either counter. Replays are served from the pack's fixtures with no
model call, so a demonstration keeps working after the ceiling is spent. Only
free-form input, which actually costs money, is metered.

In-process counters, no dependency. That is correct for a single worker and
wrong the moment there are several -- at which point the counters belong in
something shared, and this module is the only thing that has to change.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""
    retry_after_s: int = 0


class RateLimiter:
    def __init__(self, per_minute: int, daily_cap: int) -> None:
        self._per_minute = per_minute
        self._daily_cap = daily_cap
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._day = 0
        self._today = 0
        self._lock = threading.Lock()

    def check(self, client: str, *, now: float | None = None) -> Verdict:
        now = time.time() if now is None else now
        with self._lock:
            day = int(now // SECONDS_PER_DAY)
            if day != self._day:
                self._day, self._today = day, 0

            if self._today >= self._daily_cap:
                # Seconds until the next UTC day boundary.
                return Verdict(False, "daily_cap", int((day + 1) * SECONDS_PER_DAY - now))

            window = self._windows[client]
            cutoff = now - 60
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self._per_minute:
                return Verdict(False, "per_minute", max(1, int(window[0] + 60 - now)))

            window.append(now)
            self._today += 1

            # Keep the address table from growing without bound on a long
            # uptime: any client with an empty window is no longer rate-limited
            # by definition, so its entry carries no information.
            if len(self._windows) > 4096:
                for key in [k for k, v in self._windows.items() if not v]:
                    del self._windows[key]

            return Verdict(True)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "per_minute": self._per_minute,
                "daily_cap": self._daily_cap,
                "used_today": self._today,
                "tracked_clients": len(self._windows),
            }
