"""Every tunable lives here and is read from the environment.

Nothing downstream hardcodes a model id, a limit, or a timeout. Swapping the
model, the reasoning depth, or the scenario is an environment change, not a
code change -- which is also what makes `tools/bench.py` able to sweep them.
"""

from __future__ import annotations

import pathlib
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolved against this package, not the working directory. A relative ".env"
# is found only when the process happens to be launched from the project root,
# so it works locally and then quietly does not under a process manager that
# sets its own cwd -- and the failure is silent: every setting falls back to
# its default and the app serves the wrong scenario without complaining.
ENV_FILE = pathlib.Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # -- which pack to serve ------------------------------------------------
    scenario: str = "airport"

    # -- model --------------------------------------------------------------
    anthropic_api_key: str = ""
    model: str = "claude-opus-5"

    # Reasoning depth. `low` is deliberate, not a cost compromise: routing one
    # utterance to one tool is a shallow decision, and on this class of task
    # low effort matches higher settings while spending a fraction of the
    # tokens. Raise it if a pack's disambiguation rules get genuinely subtle.
    effort: str = "low"

    # Thinking stays ON. Turning it off is the obvious latency move and it is a
    # trap on this model: with thinking disabled, a forced tool call can come
    # back as plain text in the visible response -- the turn succeeds, the tool
    # never runs, and nothing raises. Low effort buys the same savings without
    # that failure mode. See docs/LATENCY.md.
    thinking: str = "adaptive"

    # Caps thinking + response text together. The routing payload is small, but
    # leave headroom so a long restatement in a verbose language cannot truncate.
    max_tokens: int = 8192

    # Fail fast rather than freeze. The SDK's default backoff on a 429 reads on
    # screen as a hung app; surfacing the throttle immediately lets the caller
    # show a recoverable state instead of a spinner.
    max_retries: int = 0
    request_timeout_s: float = 30.0

    # -- transport ----------------------------------------------------------
    streaming: bool = True

    # -- guards -------------------------------------------------------------
    # Per-IP sliding window plus a whole-deployment daily ceiling. Canned
    # scenario replays are served from fixtures and never counted, so a demo
    # keeps working after the ceiling is reached.
    rate_limit_per_min: int = 30
    rate_limit_daily: int = 1000
    max_utterance_chars: int = 2000
    max_history_turns: int = 6

    # -- http ---------------------------------------------------------------
    cors_origins: str = "http://localhost:5173,http://localhost:4173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
