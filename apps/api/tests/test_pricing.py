"""Cost is charged at the rate in force on the day of the call.

Claude Sonnet 5 launched on introductory pricing that expires. Writing the promo
rate in as the standard one under-reports every document from the day it lapses,
in the direction that flatters the project, and nothing fails to say so — so the
expiry date is data, and these tests pin both sides of it.
"""

from __future__ import annotations

from datetime import date

from app.core.claude import estimate_cost_usd

MTOK = 1_000_000
DURING = date(2026, 8, 1)
LAST_DAY = date(2026, 8, 31)
AFTER = date(2026, 9, 1)


def test_sonnet_5_bills_the_introductory_rate_during_the_window() -> None:
    assert estimate_cost_usd("claude-sonnet-5", MTOK, 0, on=DURING) == 2.00
    assert estimate_cost_usd("claude-sonnet-5", 0, MTOK, on=DURING) == 10.00


def test_the_last_day_is_inclusive() -> None:
    assert estimate_cost_usd("claude-sonnet-5", MTOK, 0, on=LAST_DAY) == 2.00


def test_the_standard_rate_resumes_the_next_morning() -> None:
    """The self-correction: no code change, no diary entry, no silent drift."""
    assert estimate_cost_usd("claude-sonnet-5", MTOK, 0, on=AFTER) == 3.00
    assert estimate_cost_usd("claude-sonnet-5", 0, MTOK, on=AFTER) == 15.00


def test_models_without_a_promotion_are_date_independent() -> None:
    for when in (DURING, LAST_DAY, AFTER):
        assert estimate_cost_usd("claude-haiku-4-5", MTOK, 0, on=when) == 1.00
        assert estimate_cost_usd("claude-opus-5", 0, MTOK, on=when) == 25.00


def test_an_unknown_model_falls_back_rather_than_reporting_zero() -> None:
    """A missing entry must never make a real call look free."""
    assert estimate_cost_usd("claude-not-a-model", MTOK, MTOK, on=DURING) == 18.00


def test_a_call_with_no_tokens_costs_nothing() -> None:
    assert estimate_cost_usd("claude-sonnet-5", 0, 0, on=DURING) == 0.0
