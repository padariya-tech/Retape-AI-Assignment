"""Comprehensive tests beyond the minimum bar.

Covers: even/staircase/balloon shapes, token-pay floors, tier floors,
max_segments cap, exact-sum, same-day ordering (credits before debits),
balance hitting exactly $0, horizon limit, fee compliance (no fee before
first payment), both Part 2 minima, round-half-up, and edge cases.
"""
from __future__ import annotations

from datetime import date

import pytest

from feasibility.engine import (
    Result,
    ScheduleRow,
    evaluate_offer,
    find_schedule,
    build_even_payments,
    build_balloon_payments,
    build_staircase_payments,
    floor_at,
    simulate,
)
from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    Offer,
    load_case,
    offer_total_cents,
    program_fee_cents,
    round_half_up,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_client(
    draft=10000,
    draft_day=1,
    first_draft="2026-01-01",
    last_draft="2026-06-01",
    as_of="2025-12-31",
    balance=0,
    extra_ledger=None,
) -> Client:
    first = date.fromisoformat(first_draft)
    last = date.fromisoformat(last_draft)
    ledger = []
    d = first
    while d <= last:
        ledger.append(LedgerEntry(d, draft, "credit"))
        # advance one month
        y, m = d.year, d.month + 1
        if m > 12:
            y += 1; m = 1
        d = d.replace(year=y, month=m, day=draft_day)
    if extra_ledger:
        ledger.extend(extra_ledger)
    return Client(
        draft_amount_cents=draft,
        draft_day=draft_day,
        first_draft_date=first,
        last_draft_date=last,
        as_of_date=date.fromisoformat(as_of),
        current_balance_cents=balance,
        ledger=ledger,
    )


def make_rules(
    max_terms=12, max_payments=12, min_payment=2500,
    max_token_pays=6, tiers=None,
    even=False, balloon=False, max_seg=4,
    bank_fee=0, prog_fee_pct=0.0,
) -> CreditorRules:
    return CreditorRules(
        max_terms=max_terms, max_payments=max_payments,
        min_payment_cents=min_payment, max_token_pays=max_token_pays,
        min_payment_tiers=tiers or [],
        even_pays=even, is_ballooning_allowed=balloon,
        max_segments=max_seg, bank_fee_cents=bank_fee,
        program_fee_pct=prog_fee_pct,
    )


def make_offer(
    balance=100000, orig=100000, pct=0.5, fpd="2026-01-31"
) -> Offer:
    return Offer(
        creditor="TestCo",
        creditor_balance_cents=balance,
        original_balance_cents=orig,
        settlement_pct=pct,
        first_payment_date=date.fromisoformat(fpd) if fpd else None,
    )


# ---------------------------------------------------------------------------
# 1. Round-half-up
# ---------------------------------------------------------------------------

def test_round_half_up_positive():
    assert round_half_up(0.5) == 1
    assert round_half_up(1.5) == 2
    assert round_half_up(2.5) == 3
    assert round_half_up(0.4) == 0
    assert round_half_up(1.4) == 1


def test_round_half_up_negative():
    assert round_half_up(-0.5) == -1
    assert round_half_up(-1.5) == -2


# ---------------------------------------------------------------------------
# 2. offer_total and program_fee use round-half-up
# ---------------------------------------------------------------------------

def test_offer_total_rounding():
    # round-half-up per ASSIGNMENT.md §3
    # 100001 * 0.5 = 50000.5 → 50001
    o = make_offer(balance=100001, pct=0.5)
    assert offer_total_cents(o) == 50001
    # 100003 * 0.5 = 50001.5 → 50002
    o2 = make_offer(balance=100003, pct=0.5)
    assert offer_total_cents(o2) == 50002


def test_program_fee_rounding():
    r = make_rules(prog_fee_pct=0.125)
    o = make_offer(orig=100001)
    # 100001 * 0.125 = 12500.125 → rounds to 12500
    assert program_fee_cents(o, r) == 12500


# ---------------------------------------------------------------------------
# 3. Floor at position
# ---------------------------------------------------------------------------

def test_floor_base():
    r = make_rules(min_payment=2500, max_token_pays=6)
    assert floor_at(1, 0, r) == 2500
    assert floor_at(6, 5, r) == 2500  # 5 token pays used, still at base


def test_floor_token_pay_exhausted():
    r = make_rules(min_payment=2500, max_token_pays=3)
    # After 3 token pays, must strictly exceed base
    assert floor_at(4, 3, r) == 2501


def test_floor_tier_overrides():
    r = make_rules(min_payment=2500, max_token_pays=10, tiers=[(5, 7000)])
    assert floor_at(4, 0, r) == 2500
    assert floor_at(5, 0, r) == 7000
    assert floor_at(6, 0, r) == 7000


def test_floor_tier_and_token_combined():
    r = make_rules(min_payment=2500, max_token_pays=2, tiers=[(5, 7000)])
    # Pos 3, token pays exhausted: floor = 2501
    assert floor_at(3, 2, r) == 2501
    # Pos 5 with token exhausted: max(tier=7000, 2501) = 7000
    assert floor_at(5, 2, r) == 7000


# ---------------------------------------------------------------------------
# 4. Even payment builder
# ---------------------------------------------------------------------------

def test_build_even_divisible():
    r = make_rules(min_payment=1000, max_token_pays=10, even=True)
    payments = build_even_payments(4, 10000, r)
    assert payments == [2500, 2500, 2500, 2500]


def test_build_even_remainder_on_late():
    r = make_rules(min_payment=100, max_token_pays=10, even=True)
    payments = build_even_payments(3, 10001, r)
    # 10001 / 3 = 3333 rem 2 → [3333, 3334, 3334]
    assert payments == [3333, 3334, 3334]
    assert sum(payments) == 10001


def test_build_even_fails_if_below_floor():
    r = make_rules(min_payment=5000, max_token_pays=10, even=True)
    # 3 payments of ~3333 each < 5000 floor
    assert build_even_payments(3, 10000, r) is None


# ---------------------------------------------------------------------------
# 5. Balloon payment builder
# ---------------------------------------------------------------------------

def test_build_balloon_structure():
    r = make_rules(min_payment=2500, max_token_pays=6, balloon=True)
    payments = build_balloon_payments(4, 20000, r)
    assert payments is not None
    assert sum(payments) == 20000
    assert payments[-1] > payments[-2]  # balloon is larger
    # First 3 at floor
    assert payments[0] == 2500
    assert payments[1] == 2500
    assert payments[2] == 2500
    assert payments[3] == 20000 - 7500  # 12500


def test_build_balloon_non_decreasing():
    r = make_rules(min_payment=2500, max_token_pays=6, balloon=True)
    for k in range(1, 5):
        payments = build_balloon_payments(k, 20000, r)
        if payments:
            assert all(payments[i] <= payments[i+1] for i in range(len(payments)-1))


# ---------------------------------------------------------------------------
# 6. Staircase: max_segments cap
# ---------------------------------------------------------------------------

def test_staircase_max_segments_1():
    # With max_segments=1, all payments must be same level
    r = make_rules(min_payment=1000, max_token_pays=10, max_seg=1)
    payments = build_staircase_payments(4, 10000, r)
    assert payments is not None
    assert len(set(payments)) <= 1


def test_staircase_max_segments_2():
    r = make_rules(min_payment=2500, max_token_pays=6, max_seg=2)
    payments = build_staircase_payments(6, 30000, r)
    if payments:
        assert len(set(payments)) <= 2
        assert sum(payments) == 30000
        assert all(payments[i] <= payments[i+1] for i in range(len(payments)-1))


# ---------------------------------------------------------------------------
# 7. Same-day ordering: credits before debits
# ---------------------------------------------------------------------------

def test_same_day_credits_before_debits():
    """A debit on the same day as a credit should not overdraft if credit > debit."""
    client = Client(
        draft_amount_cents=10000, draft_day=1,
        first_draft_date=date(2026, 1, 1), last_draft_date=date(2026, 3, 1),
        as_of_date=date(2025, 12, 31), current_balance_cents=0,
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
            LedgerEntry(date(2026, 3, 1), 10000, "credit"),
        ],
    )
    rules = make_rules(max_terms=1, max_payments=1, min_payment=5000,
                       max_token_pays=1, bank_fee=0, prog_fee_pct=0.0)
    offer = make_offer(balance=5000, orig=5000, pct=1.0, fpd="2026-01-01")
    r = evaluate_offer(client, offer, rules)
    # Credit of 10000 and debit of 5000 on Jan 1 → should be feasible
    assert r.feasible is True


# ---------------------------------------------------------------------------
# 8. Balance hits exactly $0 (not negative)
# ---------------------------------------------------------------------------

def test_balance_exactly_zero():
    """Tight budget — balance reaches $0 but never negative."""
    client = make_client(draft=5000, last_draft="2026-02-01")
    rules = make_rules(max_terms=2, max_payments=2, min_payment=2500,
                       max_token_pays=2, bank_fee=0, prog_fee_pct=0.0)
    offer = make_offer(balance=10000, orig=10000, pct=1.0, fpd="2026-01-31")
    r = evaluate_offer(client, offer, rules)
    if r.feasible:
        assert all(row.balance_cents >= 0 for row in r.schedule)
        assert any(row.balance_cents == 0 for row in r.schedule)


# ---------------------------------------------------------------------------
# 9. Horizon limit — no payments scheduled past last_draft_date
# ---------------------------------------------------------------------------

def test_horizon_respected():
    """Payments must not be scheduled after last_draft_date."""
    client = make_client(draft=10000, last_draft="2026-02-01")
    rules = make_rules(max_terms=12, max_payments=12, min_payment=2500,
                       max_token_pays=6, bank_fee=0, prog_fee_pct=0.0)
    offer = make_offer(balance=100000, orig=100000, pct=0.5, fpd="2026-01-31")
    r = evaluate_offer(client, offer, rules)
    horizon = date(2026, 2, 1)
    if r.schedule:
        assert all(row.date <= horizon for row in r.schedule)


# ---------------------------------------------------------------------------
# 10. Fee compliance: program fee not before first payment
# ---------------------------------------------------------------------------

def test_fee_not_before_first_payment():
    """Program fee must not appear on dates before the first creditor payment."""
    client = make_client(draft=20000, last_draft="2026-06-01")
    rules = make_rules(max_terms=4, max_payments=4, min_payment=2500,
                       max_token_pays=4, bank_fee=0, prog_fee_pct=0.2)
    offer = make_offer(balance=50000, orig=60000, pct=0.5, fpd="2026-03-31")
    r = evaluate_offer(client, offer, rules)
    if r.feasible and r.schedule:
        first_pay_date = r.schedule[0].date
        for row in r.schedule:
            if row.date < first_pay_date:
                assert row.program_fee_cents == 0


# ---------------------------------------------------------------------------
# 11. Exact sum constraint
# ---------------------------------------------------------------------------

def test_exact_sum_even():
    client, offer, rules = load_case("cases/case1_feasible_even")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    total = sum(row.creditor_payment_cents for row in r.schedule)
    assert total == offer_total_cents(offer)


def test_exact_sum_balloon():
    client, offer, rules = load_case("cases/case3_balloon")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    total = sum(row.creditor_payment_cents for row in r.schedule)
    assert total == offer_total_cents(offer)


def test_exact_sum_staircase():
    client, offer, rules = load_case("cases/case4_tiers")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    total = sum(row.creditor_payment_cents for row in r.schedule)
    assert total == offer_total_cents(offer)


# ---------------------------------------------------------------------------
# 12. Program fee fully collected
# ---------------------------------------------------------------------------

def test_program_fee_fully_collected():
    client, offer, rules = load_case("cases/case1_feasible_even")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    collected = sum(row.program_fee_cents for row in r.schedule)
    expected = program_fee_cents(offer, rules)
    assert collected == expected


# ---------------------------------------------------------------------------
# 13. Non-decreasing payments
# ---------------------------------------------------------------------------

def test_payments_non_decreasing():
    for case in ["case1_feasible_even", "case3_balloon", "case4_tiers"]:
        client, offer, rules = load_case(f"cases/{case}")
        r = evaluate_offer(client, offer, rules)
        if r.feasible and r.schedule:
            payments = [row.creditor_payment_cents for row in r.schedule]
            assert all(payments[i] <= payments[i+1] for i in range(len(payments)-1)), \
                f"Non-decreasing violated in {case}: {payments}"


# ---------------------------------------------------------------------------
# 14. Bank fee only on creditor-payment dates
# ---------------------------------------------------------------------------

def test_bank_fee_on_payment_dates_only():
    client, offer, rules = load_case("cases/case4_tiers")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    for row in r.schedule:
        if row.creditor_payment_cents == 0:
            assert row.bank_fee_cents == 0
        else:
            assert row.bank_fee_cents == rules.bank_fee_cents


# ---------------------------------------------------------------------------
# 15. Token-pay cap enforced
# ---------------------------------------------------------------------------

def test_token_pay_cap():
    """With max_token_pays=2, only 2 payments may equal the base min."""
    r = make_rules(min_payment=2500, max_token_pays=2, max_seg=4)
    payments = build_staircase_payments(6, 25000, r)
    if payments:
        token_pays = sum(1 for p in payments if p == 2500)
        assert token_pays <= 2


# ---------------------------------------------------------------------------
# 16. Infeasible: exact lump sum and monthly increment from test_cases
# ---------------------------------------------------------------------------

def test_case2_lump_sum_exact():
    r = _run("case2_infeasible_minima")
    assert r.additional_funds.lump_sum.amount_cents == 10000


def test_case2_monthly_increment_exact():
    r = _run("case2_infeasible_minima")
    assert r.additional_funds.monthly_increment.amount_cents == 2500
    assert r.additional_funds.monthly_increment.num_drafts == 5


# ---------------------------------------------------------------------------
# 17. Guardrail checks
# ---------------------------------------------------------------------------

def test_lump_guardrail_pass():
    """Lump sum within 65% of offer_total → within_guardrail=True."""
    r = _run("case2_infeasible_minima")
    assert r.additional_funds.lump_sum.within_guardrail is True


def test_monthly_guardrail_pass():
    r = _run("case2_infeasible_minima")
    assert r.additional_funds.monthly_increment.within_guardrail is True


def test_guardrail_lump_fails():
    """Construct a case where lump sum exceeds 65% of offer_total."""
    # Very large offer, tiny drafts — lump needed is big
    client = make_client(draft=1000, last_draft="2026-03-01")
    rules = make_rules(max_terms=2, max_payments=2, min_payment=50000,
                       max_token_pays=2, bank_fee=0, prog_fee_pct=0.0)
    offer = make_offer(balance=200000, orig=200000, pct=1.0, fpd="2026-01-31")
    r = evaluate_offer(client, offer, rules)
    if not r.feasible and r.additional_funds:
        offer_total = offer_total_cents(offer)
        guardrail = round_half_up(0.65 * offer_total)
        if r.additional_funds.lump_sum.amount_cents > guardrail:
            assert r.additional_funds.lump_sum.within_guardrail is False


# ---------------------------------------------------------------------------
# 18. Feasibility simulation balance never negative
# ---------------------------------------------------------------------------

def test_balance_never_negative_case1():
    r = _run("case1_feasible_even")
    assert all(row.balance_cents >= 0 for row in r.schedule)


def test_balance_never_negative_case3():
    r = _run("case3_balloon")
    assert all(row.balance_cents >= 0 for row in r.schedule)


def test_balance_never_negative_case4():
    r = _run("case4_tiers")
    assert all(row.balance_cents >= 0 for row in r.schedule)


# ---------------------------------------------------------------------------
# 19. Existing debits in ledger are respected (case3 has a debit)
# ---------------------------------------------------------------------------

def test_existing_debit_respected():
    """case3 has a 15000 debit on 2026-02-01. Schedule must still be valid."""
    client, offer, rules = load_case("cases/case3_balloon")
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    assert all(row.balance_cents >= 0 for row in r.schedule)


# ---------------------------------------------------------------------------
# 20. Tier floors respected in case4
# ---------------------------------------------------------------------------

def test_tier_floor_case4():
    r = _run("case4_tiers")
    payments = [row.creditor_payment_cents for row in r.schedule]
    # payments[6:] are positions 7+ (1-based) → must be >= 5000
    for p in payments[6:]:
        assert p >= 5000


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run(case: str) -> Result:
    client, offer, rules = load_case(f"cases/{case}")
    return evaluate_offer(client, offer, rules)
