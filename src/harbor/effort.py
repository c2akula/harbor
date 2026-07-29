"""Effort → thinking budget: the one place these numbers live.

The user-facing dial is a label (config `[endpoint] effort`); the token
numbers behind it are a tuning table, revised by measurement, never repeated
elsewhere. `max` means uncapped — the model may spend the whole output cap
thinking, which can return a reasoning-only reply; every other level reserves
answer room.
"""
from __future__ import annotations

# Per-request output cap — the model card's recommendation for most queries.
MAX_TOKENS = 32768
# Tokens of the cap that thinking may never consume.
ANSWER_RESERVE = 8192

# Labels are a subset of the vLLM/Claude Code effort vocabulary. They map to
# vLLM's thinking_token_budget rather than its reasoning_effort param, which
# for chat-template models collapses to enable_thinking on/off — a dial that
# looks graded but is not.
BUDGETS: dict[str, int | None] = {
    "none": 0,                             # thinking disabled
    "low": 4096,
    "medium": 12288,
    "high": MAX_TOKENS - ANSWER_RESERVE,   # 24576 — the default
    "max": None,                           # uncapped
}

DEFAULT = "high"


def budget(level: str) -> int | None:
    """Thinking-token budget for an effort label; None = uncapped."""
    try:
        return BUDGETS[level]
    except KeyError:
        raise ValueError(
            f"unknown effort level {level!r} — one of: "
            f"{', '.join(BUDGETS)}") from None
