"""Settlement feasibility & fee engine implementation.

Objective: collect program fee as early as possible (front-loaded).
This naturally pushes creditor payments to be as low as rules allow early,
and larger later — so the surplus each month can absorb program fees first.

Payment shapes:
- even: all creditor payments equal (even_pays=True)
- balloon: min payments early, final payment absorbs remainder (is_ballooning_allowed=True)
- staircase: payments step up, at most max_segments distinct levels

For each k (number of payments, tried from max down to 1):
  1. Build candidate creditor payment vector respecting floors.
  2. Distribute program fee greedily front-loaded.
  3. Simulate ledger. If balance never goes negative → feasible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Optional

from feasibility.models import (
    Client,
    CreditorRules,
    Offer,
    LedgerEntry,
    add_months,
    default_first_payment_date,
    end_of_month,
    is_end_of_month,
    monthly_payment_dates,
    offer_total_cents,
    program_fee_cents,
    round_half_up,
)


# ---------------------------------------------------------------------------
# Output dataclasses (keep in sync with ASSIGNMENT.md)
# ---------------------------------------------------------------------------

@dataclass
class ScheduleRow:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


@dataclass
class FundsOption:
    amount_cents: int
    within_guardrail: bool
    reason: str
    date: date | None = None
    num_drafts: int | None = None


@dataclass
class AdditionalFunds:
    lump_sum: FundsOption
    monthly_increment: FundsOption


@dataclass
class Result:
    feasible: bool
    pay_shape_used: str | None = None
    schedule: list[ScheduleRow] | None = None
    additional_funds: AdditionalFunds | None = None

    def to_dict(self) -> dict:
        out: dict = {"feasible": self.feasible, "pay_shape_used": self.pay_shape_used}
        out["schedule"] = (
            [
                {
                    "date": r.date.isoformat(),
                    "creditor_payment_cents": r.creditor_payment_cents,
                    "program_fee_cents": r.program_fee_cents,
                    "bank_fee_cents": r.bank_fee_cents,
                    "balance_cents": r.balance_cents,
                }
                for r in self.schedule
            ]
            if self.schedule is not None
            else None
        )
        if self.additional_funds is None:
            out["additional_funds"] = None
        else:
            def opt(o: FundsOption) -> dict:
                d = {
                    "amount_cents": o.amount_cents,
                    "within_guardrail": o.within_guardrail,
                    "reason": o.reason,
                }
                if o.date is not None:
                    d["date"] = o.date.isoformat()
                if o.num_drafts is not None:
                    d["num_drafts"] = o.num_drafts
                return d

            out["additional_funds"] = {
                "lump_sum": opt(self.additional_funds.lump_sum),
                "monthly_increment": opt(self.additional_funds.monthly_increment),
            }
        return out


# ---------------------------------------------------------------------------
# Floor calculation for a payment at position i (1-based)
# ---------------------------------------------------------------------------

def floor_at(i: int, token_pays_used: int, rules: CreditorRules) -> int:
    """Minimum allowed creditor payment at 1-based position i."""
    base = rules.min_payment_cents

    # token-pay rule: if we've already used max_token_pays payments at base min,
    # subsequent payments must strictly exceed base min
    if token_pays_used >= rules.max_token_pays:
        base = base + 1

    # tier floors
    for from_pos, tier_min in rules.min_payment_tiers:
        if i >= from_pos:
            base = max(base, tier_min)

    return base


# ---------------------------------------------------------------------------
# Build creditor payment vectors for each shape
# ---------------------------------------------------------------------------

def build_even_payments(k: int, offer_total: int, rules: CreditorRules) -> list[int] | None:
    """Equal payments, remainder distributed to latest (non-decreasing)."""
    base = offer_total // k
    remainder = offer_total - base * k
    # last `remainder` payments get base+1
    payments = [base] * (k - remainder) + [base + 1] * remainder

    # Validate floors — even_pays means floors still apply (token-pay cap etc.)
    token_pays_used = 0 # count of token pays used so far (to track token-pay cap)
    for i, p in enumerate(payments, 1):
        fl = floor_at(i, token_pays_used, rules)
        if p < fl:
            return None
        if p == rules.min_payment_cents:
            token_pays_used += 1

    # Non-decreasing check (guaranteed by construction since base ≤ base+1)
    return payments


def build_balloon_payments(k: int, offer_total: int, rules: CreditorRules) -> list[int] | None:
    """Minimum payments for payments 1..k-1, final payment absorbs the rest.

    Interpretation: to front-load fees, we keep creditor payments as low as
    possible for all but the last payment. The last payment is whatever remains
    (the "balloon"). We still respect floors and non-decreasing at each step.
    """
    payments: list[int] = []
    remaining = offer_total
    token_pays_used = 0

    for i in range(1, k):
        fl = floor_at(i, token_pays_used, rules)
        p = fl
        remaining -= p
        if p == rules.min_payment_cents:
            token_pays_used += 1
        payments.append(p)

    # Balloon: all remaining goes to final payment
    balloon = remaining
    if balloon <= 0:
        return None  # can't have non-positive balloon
    fl_last = floor_at(k, token_pays_used, rules)
    if balloon < fl_last:
        return None
    # Non-decreasing: balloon must be >= last payment
    if payments and balloon < payments[-1]:
        return None
    payments.append(balloon)
    return payments


def build_staircase_payments(k: int, offer_total: int, rules: CreditorRules) -> list[int] | None:
    """Step-up staircase with at most max_segments distinct levels.

    Objective: keep early payments low (to allow more program-fee collection early).
    Strategy: fill as many early positions as possible with floor amounts, then
    step up to cover the remaining balance. With max_segments=2, this is:
    - Segment 1: floor payments for positions 1..j
    - Segment 2: a larger equal amount for positions j+1..k
    We try all possible split points j and pick the one that's valid.
    For max_segments > 2, we generalize recursively.

    Simpler approach that generalizes: greedily assign floors early, then
    set remaining payments to ceil(remaining/remaining_slots) or similar.
    We try all segment-split configurations.
    """
    max_seg = rules.max_segments

    # If only 1 segment, all payments must be equal
    if max_seg == 1:
        return build_even_like(k, offer_total, rules)

    # Try splits: positions 1..split1 at floor-level, rest at higher level
    # For max_segments=2: split at j means levels [floor, higher]
    # We try j from 0 to k-1 and pick j that is feasible
    return _try_staircase(k, offer_total, rules, max_seg)


def build_even_like(k: int, offer_total: int, rules: CreditorRules) -> list[int] | None:
    """All equal, floor-valid."""
    base = offer_total // k
    remainder = offer_total - base * k
    payments = [base] * (k - remainder) + [base + 1] * remainder
    token_pays_used = 0
    for i, p in enumerate(payments, 1):
        fl = floor_at(i, token_pays_used, rules)
        if p < fl:
            return None
        if p == rules.min_payment_cents:
            token_pays_used += 1
    return payments

def count_segments(payments: list[int]) -> int:
    if not payments:
        return 0

    segments = 1

    for i in range(1, len(payments)):
        # Ignore +/-1 caused by remainder distribution
        if abs(payments[i] - payments[i - 1]) > 2:
            segments += 1

    return segments

def _try_staircase(k: int, offer_total: int, rules: CreditorRules, max_seg: int) -> list[int] | None:
    """
    Greedily: put as many floor-level payments first as possible, then step up.
    With max_segments = S, we have up to S distinct levels.

    We do a simple two-pass: 
    1. Assign floor to first n positions (maximizing n for front-loading)
    2. Distribute remainder evenly across remaining positions (one more level)
    3. Repeat subdivision if max_segments allows

    For correctness and simplicity, we enumerate split points.
    """
    best = None

    # Try each split point j: j positions at floor level, k-j at higher level
    # We pick j from k-1 down to 0 (prefer more early floors = more fee front-loading)
    for j in range(k - 1, -1, -1):
        # Compute floors for first j positions
        early_payments: list[int] = []
        token_pays_used = 0
        valid_early = True
        for i in range(1, j + 1):
            fl = floor_at(i, token_pays_used, rules)
            p = fl
            early_payments.append(p)
            if p == rules.min_payment_cents:
                token_pays_used += 1

        early_sum = sum(early_payments)
        remaining_total = offer_total - early_sum
        remaining_slots = k - j

        if remaining_slots <= 0:
            # All payments at floor — check exact sum
            if early_sum == offer_total:
                payments = early_payments
                if _validate_payments(payments, rules):
                    return payments
            continue

        if remaining_total <= 0:
            continue

        # Distribute remaining_total across remaining_slots as evenly as possible
        # (non-decreasing means each late payment >= previous)
        base_late = remaining_total // remaining_slots
        rem = remaining_total - base_late * remaining_slots
        late_payments = [base_late] * (remaining_slots - rem) + [base_late + 1] * rem

        # Check floor for late payments
        late_valid = True
        for idx, p in enumerate(late_payments):
            pos = j + 1 + idx
            fl = floor_at(pos, token_pays_used, rules)
            if p < fl:
                late_valid = False
                break
            if p == rules.min_payment_cents:
                token_pays_used += 1

        if not late_valid:
            continue

        payments = early_payments + late_payments

        # Non-decreasing check
        if not all(payments[i] <= payments[i + 1] for i in range(len(payments) - 1)):
            continue

        # Segment count check
        # distinct = len(set(payments))
        distinct = count_segments(payments)
        if distinct > max_seg:
            continue

        if sum(payments) != offer_total:
            continue

        if _validate_payments(payments, rules):
            return payments

    return None


def _validate_payments(payments: list[int], rules: CreditorRules) -> bool:
    """Check all hard constraints on payment vector (except SDA simulation)."""
    if not payments:
        return False
    k = len(payments)
    if k < 1 or k > min(rules.max_payments, rules.max_terms):
        return False

    token_pays_used = 0
    prev = 0
    for i, p in enumerate(payments, 1):
        # Non-decreasing
        if p < prev:
            return False
        # Floor
        fl = floor_at(i, token_pays_used, rules)
        if p < fl:
            return False
        if p == rules.min_payment_cents:
            token_pays_used += 1
        prev = p

    return True


# ---------------------------------------------------------------------------
# Ledger simulation
# ---------------------------------------------------------------------------

def simulate(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    payment_dates: list[date],
    creditor_payments: list[int],
    extra_credits: list[tuple[date, int]] | None = None,
    extra_draft_amount: int = 0,
) -> tuple[bool, list[ScheduleRow]]:
    """
    Simulate the full ledger. Returns (feasible, schedule_rows).

    extra_credits: additional credit entries (lump sum scenario)
    extra_draft_amount: additional cents added to each future draft
    """
    total_program_fee = program_fee_cents(offer, rules)

    # Build a map of all events by date
    # Events: (date, type, amount)
    # Credits come before debits on same day

    # --- Collect all entries ---
    from collections import defaultdict
    credits_by_date: dict[date, int] = defaultdict(int)
    debits_by_date: dict[date, int] = defaultdict(int)

    # Existing ledger entries (after as_of_date are the modifiable future)
    for entry in client.ledger:
        if entry.date > client.as_of_date:
            if entry.type == "credit":
                credits_by_date[entry.date] += entry.amount_cents
                # Apply extra draft amount to future draft credits
                if extra_draft_amount > 0 and entry.amount_cents == client.draft_amount_cents:
                    credits_by_date[entry.date] += extra_draft_amount
            else:
                debits_by_date[entry.date] += entry.amount_cents
        # entries on or before as_of_date are baked into current_balance_cents

    # Extra lump-sum credits
    if extra_credits:
        for d, amt in extra_credits:
            credits_by_date[d] += amt

    # Mark creditor payment dates and amounts
    k = len(payment_dates)
    assert len(creditor_payments) == k

    # Build per-date totals for creditor payments and bank fees
    creditor_by_date: dict[date, int] = defaultdict(int)
    bankfee_by_date: dict[date, int] = defaultdict(int)
    for d, cp in zip(payment_dates, creditor_payments):
        creditor_by_date[d] += cp
        bankfee_by_date[d] += rules.bank_fee_cents  # bank fee on each payment date

    # Collect all dates we need to process
    all_dates = sorted(set(
        list(credits_by_date.keys()) +
        list(debits_by_date.keys()) +
        list(payment_dates)
    ))

    # Program fee distribution: front-load starting from first_payment_date
    # We assign program fee greedily: on each cadence date (from first payment date onward),
    # assign as much fee as possible given the available balance.
    # We do two passes: first compute balances without program fee, then distribute.
    # Actually we need to interleave. Use a greedy single-pass:
    # At each payment date, after creditor payment + bank fee, assign available balance as fee.

    # But "available balance" also depends on future dates. The correct approach:
    # Since we want fee front-loaded and balance >= 0 at ALL dates,
    # we must be careful. We can't take more fee than what leaves the balance >= 0
    # for all future debits. However, since program fee is ours to schedule, we greedily
    # take as much as possible on each cadence date without making any future date go negative.

    # Simpler correct approach for single-pass (greedy):
    # On each cadence date, assign fee = min(remaining_fee, surplus_after_mandatory_debits)
    # where surplus = balance_after_credits_and_mandatory_debits.
    # "Mandatory debits" = creditor payments + bank fees + existing ledger debits.
    # Program fees are flexible — we assign them greedily.

    # Two-pass: first compute balance trajectory with NO program fee,
    # then greedily assign fee on each cadence date.

    fee_dates = payment_dates  # program fee only allowed from first payment date onward

    # Pass 1: compute balance trajectory without program fee
    balance = client.current_balance_cents
    balance_without_fee: dict[date, int] = {}
    for d in all_dates:
        balance += credits_by_date.get(d, 0)
        balance -= debits_by_date.get(d, 0)
        balance -= creditor_by_date.get(d, 0)
        balance -= bankfee_by_date.get(d, 0)
        balance_without_fee[d] = balance
        if balance < 0:
            # Won't be feasible, but continue to compute correctly
            pass

    # Pass 2: greedily assign program fee on cadence dates
    # We assign fee on each cadence date = min(remaining_fee, available_surplus)
    # where available_surplus = balance at that date (after mandatory debits) 
    # but we also need to not make any FUTURE date go negative due to fee extraction.
    # Since program fee is only a debit (takes money out), taking it earlier reduces
    # future balances. We thus take min(remaining_fee, balance_at_date) where
    # balance_at_date is the running balance at that point in the simulation.

    # Actually the correct greedy: simulate forward, track running balance,
    # and at each cadence date take as much fee as possible without overdraft at that date.
    # Future dates: taking fee now reduces all future balances by that amount.
    # So: fee_taken_at_d = min(remaining_fee, balance_at_d_after_mandatory - 0)
    # But "balance_at_d_after_mandatory" at date d depends on fees already taken.

    # Let's do it in a single forward pass:
    fee_remaining = total_program_fee
    fee_schedule: dict[date, int] = defaultdict(int)

    balance = client.current_balance_cents
    running_bal: dict[date, int] = {}

    for d in all_dates:
        balance += credits_by_date.get(d, 0)
        balance -= debits_by_date.get(d, 0)
        balance -= creditor_by_date.get(d, 0)
        balance -= bankfee_by_date.get(d, 0)

        # Greedily collect program fee here if this is a cadence date
        if d in set(fee_dates) and fee_remaining > 0:
            # Take as much as possible without going negative at THIS date
            fee_here = min(fee_remaining, balance)
            if fee_here < 0:
                fee_here = 0
            fee_schedule[d] = fee_here
            fee_remaining -= fee_here
            balance -= fee_here

        running_bal[d] = balance

    feasible = all(b >= 0 for b in running_bal.values()) and fee_remaining == 0

    # Build schedule rows
    rows: list[ScheduleRow] = []
    for d, cp in zip(payment_dates, creditor_payments):
        rows.append(ScheduleRow(
            date=d,
            creditor_payment_cents=cp,
            program_fee_cents=fee_schedule.get(d, 0),
            bank_fee_cents=rules.bank_fee_cents if cp > 0 else 0,
            balance_cents=running_bal.get(d, 0),
        ))

    # Add fee-only dates if fee wasn't fully collected during payment dates
    # (fee can only be collected on cadence dates, which are exactly the payment dates here)
    # If fee_remaining > 0, try adding extra cadence dates after last payment but before horizon
    # For now the above handles it if the same payment_dates cover fee collection.

    return feasible, rows


# ---------------------------------------------------------------------------
# Core feasibility check for a given payment vector + extra credits
# ---------------------------------------------------------------------------

def try_schedule(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    k: int,
    creditor_payments: list[int],
    extra_credits: list[tuple[date, int]] | None = None,
    extra_draft_amount: int = 0,
) -> tuple[bool, list[ScheduleRow]]:
    """Try to find a feasible schedule with the given payment vector."""
    first_pdate = offer.first_payment_date or default_first_payment_date(client)
    horizon = client.last_draft_date
    payment_dates = monthly_payment_dates(first_pdate, k)

    # All payment dates must be <= horizon
    if any(d > horizon for d in payment_dates):
        return False, []

    return simulate(client, offer, rules, payment_dates, creditor_payments,
                    extra_credits=extra_credits, extra_draft_amount=extra_draft_amount)


# ---------------------------------------------------------------------------
# Find feasible schedule (try k from max down to 1)
# ---------------------------------------------------------------------------

def find_schedule(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    extra_credits: list[tuple[date, int]] | None = None,
    extra_draft_amount: int = 0,
) -> tuple[bool, list[ScheduleRow], str | None, list[int] | None]:
    """Return (feasible, rows, shape, payment_vector)."""
    total = offer_total_cents(offer)
    if total <= 0:
        return False, [], None, None

    first_pdate = offer.first_payment_date or default_first_payment_date(client)
    horizon = client.last_draft_date
    max_k = min(rules.max_payments, rules.max_terms)

    # Clamp max_k so all dates fit within horizon
    possible_dates = monthly_payment_dates(first_pdate, max_k)
    while max_k > 0 and possible_dates[max_k - 1] > horizon:
        max_k -= 1
        possible_dates = monthly_payment_dates(first_pdate, max_k)

    if max_k == 0:
        return False, [], None, None

    # Choose shape based on rules
    if rules.even_pays:
        shape = "even"
        builder = lambda k: build_even_payments(k, total, rules)
    elif rules.is_ballooning_allowed:
        shape = "balloon"
        builder = lambda k: build_balloon_payments(k, total, rules)
    else:
        shape = "staircase"
        builder = lambda k: build_staircase_payments(k, total, rules)

    # Try k from max down to 1 (more payments = more fee-collection opportunities = better front-loading)
    # Actually for fee front-loading, we want MORE time to distribute fee earlier,
    # so larger k gives more cadence dates. But also larger k means smaller early payments = more surplus early.
    # We try k from max down and return first feasible.
    for k in range(max_k, 0, -1):
        payments = builder(k)
        if payments is None:
            continue
        if sum(payments) != total:
            continue
        ok, rows = try_schedule(client, offer, rules, k, payments,
                                extra_credits=extra_credits,
                                extra_draft_amount=extra_draft_amount)
        if ok:
            return True, rows, shape, payments

    return False, [], shape, None


# ---------------------------------------------------------------------------
# Part 2: minimum additional funds
# ---------------------------------------------------------------------------

def find_min_lump_sum(client: Client, offer: Offer, rules: CreditorRules) -> FundsOption:
    """Find minimum lump sum credit that makes offer feasible."""
    # Ideal placement: as early as possible (first future date = day after as_of_date
    # or first_draft_date, whichever is sooner). We use the first draft date.
    first_future = min(
        (e.date for e in client.ledger if e.date > client.as_of_date),
        default=client.first_draft_date,
    )
    first_future = min(first_future, client.first_draft_date)

    # Binary search for minimum lump sum
    lo, hi = 0, sum(
        e.amount_cents for e in client.ledger if e.type == "credit" and e.date > client.as_of_date
    ) + offer_total_cents(offer) + program_fee_cents(offer, rules) + 1

    # Ensure hi is actually feasible
    ok, _, _, _ = find_schedule(client, offer, rules,
                                extra_credits=[(first_future, hi)])
    if not ok:
        hi *= 10  # very large upper bound

    # Binary search
    result_amount = hi
    while lo <= hi:
        mid = (lo + hi) // 2
        ok, _, _, _ = find_schedule(client, offer, rules,
                                    extra_credits=[(first_future, mid)])
        if ok:
            result_amount = mid
            hi = mid - 1
        else:
            lo = mid + 1

    offer_total = offer_total_cents(offer)
    guardrail_limit = round_half_up(0.65 * offer_total)
    within = result_amount <= guardrail_limit
    reason = "" if within else f"Lump sum {result_amount} exceeds 65% of offer total ({guardrail_limit})"

    return FundsOption(
        amount_cents=result_amount,
        within_guardrail=within,
        reason=reason,
        date=first_future,
    )


def find_min_monthly_increment(client: Client, offer: Offer, rules: CreditorRules) -> FundsOption:
    """Find minimum uniform increment X added to every future draft."""
    future_drafts = [e for e in client.ledger if e.type == "credit" and e.date > client.as_of_date]
    n = len(future_drafts)
    if n == 0:
        return FundsOption(
            amount_cents=0,
            within_guardrail=False,
            reason="No future drafts to increment",
            num_drafts=0,
        )

    # Binary search on X
    lo, hi = 0, offer_total_cents(offer) + program_fee_cents(offer, rules) + 1

    ok, _, _, _ = find_schedule(client, offer, rules, extra_draft_amount=hi)
    if not ok:
        hi *= 10

    result_x = hi
    while lo <= hi:
        mid = (lo + hi) // 2
        ok, _, _, _ = find_schedule(client, offer, rules, extra_draft_amount=mid)
        if ok:
            result_x = mid
            hi = mid - 1
        else:
            lo = mid + 1

    draft_amount = client.draft_amount_cents
    guardrail_limit = max(10000, round_half_up(0.40 * draft_amount))
    within = result_x <= guardrail_limit
    reason = "" if within else f"Monthly increment {result_x} exceeds max({10000}, 40% of draft {draft_amount}) = {guardrail_limit}"

    return FundsOption(
        amount_cents=result_x,
        within_guardrail=within,
        reason=reason,
        num_drafts=n,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    """Evaluate a single offer. See ASSIGNMENT.md for the full specification."""
    feasible, rows, shape, _ = find_schedule(client, offer, rules)

    if feasible:
        return Result(feasible=True, pay_shape_used=shape, schedule=rows, additional_funds=None)

    # Part 2: compute minimum additional funds
    lump = find_min_lump_sum(client, offer, rules)
    monthly = find_min_monthly_increment(client, offer, rules)

    return Result(
        feasible=False,
        pay_shape_used=None,
        schedule=None,
        additional_funds=AdditionalFunds(lump_sum=lump, monthly_increment=monthly),
    )
