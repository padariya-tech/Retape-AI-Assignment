# Settlement Feasibility & Fee Engine

**Candidate:** _[Your Name]_  
**Date:** _[Submission Date]_

Given a client's escrow account (SDA), a settlement offer, and creditor rules, this
engine decides whether the offer is affordable and, if so, produces a payment
schedule that front-loads the program fee. If not affordable, it computes the
minimum extra funding needed (lump sum or monthly draft increment).

Full problem specification: [`ASSIGNMENT.md`](./ASSIGNMENT.md)

---

## Quick start

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Run all tests (46 tests)
pytest -q

# Evaluate a single case
python run.py cases/case1_feasible_even
```

---

## How to submit

### What to include

Submit the **project root folder** with this structure:

```
retape_ai_takehome/
├── ASSIGNMENT.md              # problem spec (provided)
├── README.md                  # this file — your write-up
├── requirements.txt
├── run.py                     # CLI entry point
├── conftest.py
├── feasibility/
│   ├── models.py              # data models, loaders, date helpers, rounding
│   └── engine.py              # evaluate_offer implementation
├── cases/                     # four input fixtures
│   ├── case1_feasible_even/
│   ├── case2_infeasible_minima/
│   ├── case3_balloon/
│   └── case4_tiers/
└── tests/
    ├── test_smoke.py
    ├── test_cases.py          # minimum bar (4 cases)
    └── test_comprehensive.py  # edge-case coverage
```

Each case folder contains three JSON files:

```
cases/case1_feasible_even/
├── client.json           # SDA account, drafts, ledger
├── offer.json            # settlement offer
└── creditor_rules.json   # creditor-specific constraints
```

### What to exclude

Do **not** submit:

- `venv/` or `.venv/` (recreate locally with `pip install -r requirements.txt`)
- `.pytest_cache/`, `__pycache__/`, `.DS_Store`

A `.gitignore` is included in the repo for this purpose.

### Submission options

**Option A — ZIP (most common)**

From the parent directory of the project:

```bash
cd ..
zip -r retape_ai_takehome.zip "retape_ai_takehome 2" \
  -x "*/venv/*" -x "*/.pytest_cache/*" -x "*/__pycache__/*" -x "*/.DS_Store"
```

Rename the folder to `retape_ai_takehome/` (no spaces) before zipping if required.

**Option B — GitHub**

```bash
git init
git add .
git commit -m "Settlement feasibility engine take-home"
git remote add origin <your-private-repo-url>
git push -u origin main
```

Share the repo link with the reviewer. Keep the repo **private** unless told otherwise.

### Pre-submission checklist

- [ ] `pytest -q` → all tests pass
- [ ] `python run.py cases/case1_feasible_even` prints `"feasible": true`
- [ ] `python run.py cases/case2_infeasible_minima` prints lump sum `10000`, increment `2500`
- [ ] README filled in (name, date, any assumptions you made)
- [ ] No `venv/` or cache folders in the submission

---

## Verifying results against `cases/`

### Automated (recommended)

```bash
pytest tests/test_cases.py -v    # checks all 4 cases
pytest -q                        # full suite (46 tests)
```

### Manual CLI

```bash
python run.py cases/case1_feasible_even
python run.py cases/case2_infeasible_minima
python run.py cases/case3_balloon
python run.py cases/case4_tiers
```

### Expected outcomes

| Case | Verdict | Shape | Key values |
|---|---|---|---|
| case1_feasible_even | feasible | `"even"` | 6 equal payments; balances ≥ 0 |
| case2_infeasible_minima | infeasible | — | lump **$100.00** on 2026-01-01; increment **$25.00** × **5** drafts |
| case3_balloon | feasible | `"balloon"` | early payments at $25 floor; large final payment |
| case4_tiers | feasible | `"staircase"` | payments 7+ ≥ $50.00 (tier floor) |

The assignment does **not** require an exact schedule — multiple valid schedules
may exist. Tests check verdict, shape, constraint compliance, and Part 2 minima.

### Manual sanity checks (feasible cases)

For each schedule row, confirm:

1. **Exact sum** — creditor payments sum to `round_half_up(settlement_pct × creditor_balance_cents)`
2. **Fee collected** — program fees sum to `round_half_up(program_fee_pct × original_balance_cents)`
3. **Non-negative balance** — every `balance_cents ≥ 0`
4. **Non-decreasing** — each creditor payment ≥ the previous
5. **Horizon** — all dates ≤ `last_draft_date` in `client.json`
6. **Bank fee** — only charged on dates with a creditor payment

---

## Implementation

### Architecture

```
evaluate_offer(client, offer, rules)
│
├─ Part 1: find_schedule()
│   ├─ Compute offer_total and program_fee (round-half-up)
│   ├─ Determine shape from rules: even | balloon | staircase
│   ├─ For k = max_k down to 1:
│   │   ├─ Build creditor payment vector (respecting floors)
│   │   └─ simulate() → feasible? return schedule
│   └─ Return Result(feasible=True, schedule, pay_shape_used)
│
└─ Part 2 (if infeasible):
    ├─ find_min_lump_sum()      — binary search, earliest date
    ├─ find_min_monthly_increment() — binary search on draft boost
    └─ Return Result(feasible=False, additional_funds)
```

**Entry point:** `feasibility/engine.py::evaluate_offer`

**Supporting modules:**

| File | Role |
|---|---|
| `feasibility/models.py` | Dataclasses, JSON loaders, EOM cadence helpers, `round_half_up`, `offer_total_cents`, `program_fee_cents` |
| `feasibility/engine.py` | Payment builders, ledger simulation, feasibility search, Part 2 minima |
| `run.py` | CLI: load case → evaluate → print JSON |

### Core objective

Collect the **program fee as early as possible**. Early fee collection means
keeping **creditor payments as low as rules allow early on**, deferring larger
payments later. Payment shape (even / staircase / balloon) is an **output** of
this objective plus creditor flags — not a hard-coded template.

### Payment shapes

#### Even (`even_pays = true`)

All creditor payments as equal as possible. When `offer_total` is not divisible
by `k`, remainder cents go to the **latest** payments (non-decreasing).
Example: 10001 in 3 → `[3333, 3334, 3334]`.

We try `k` from `max_k` down to 1 and return the first feasible schedule.
More payments → more cadence dates → more opportunity to front-load fees.

#### Balloon (`is_ballooning_allowed = true`)

Payments 1..k−1 sit at their position-specific **floor** (base min, token-pay
cap, tier step-ups). The final payment absorbs the remainder.

This is the most aggressive fee front-loading: minimum creditor outflow early,
bulk payment at the end. Reject `k` if cumulative floors ≥ offer_total or the
balloon would violate its own floor or non-decreasing constraint.

#### Staircase (neither flag set)

At most `max_segments` distinct payment levels. We enumerate split points
`j ∈ [0, k−1]` (right to left, preferring more floor-level payments first):

- Positions 1..j at floor amounts (segment 1)
- Positions j+1..k at a higher equal amount (segment 2)

Valid splits must satisfy exact sum, floors, non-decreasing, and segment cap.
When `max_segments = 1`, this degenerates to equal payments.

### Floor calculation

For each 1-based payment position `i`:

```
floor = max(base_min, tier_min_at_i)
if token_pays_used >= max_token_pays:
    floor = max(floor, base_min + 1)   # must strictly exceed base
```

Token pays count payments sitting exactly at `min_payment_cents`.

### Program fee distribution

Collected on cadence dates on or after the first creditor payment date, fully
by the horizon. Split across dates in any non-negative amounts.

**Strategy:** greedy forward pass — on each cadence date, after mandatory debits
(creditor payment + bank fee), take `min(fee_remaining, balance)`.

### Ledger simulation

Single forward pass over all relevant dates (sorted):

1. Apply **credits** (drafts, lump sums)
2. Apply **debits** (existing ledger debits, creditor payments, bank fees)
3. Greedily collect program fee on cadence dates

Initial balance = `client.current_balance_cents` (entries on or before
`as_of_date` are already baked in). Feasible iff balance ≥ 0 at every date and
program fee fully collected.

Same-day rule: **all credits before all debits**.

### Part 2 — minimum additional funds

Both use **binary search** with `find_schedule()` as the feasibility oracle.

| Method | Placement | Guardrail |
|---|---|---|
| Lump sum | Earliest future date (min of first ledger entry after `as_of_date`, `first_draft_date`) | Reject if `L > round_half_up(0.65 × offer_total)` |
| Monthly increment | Uniform `X` added to every future draft credit | Reject if `X > max(10000, round_half_up(0.40 × draft_amount))` |

---

## Alternatives considered

| Approach | Why not chosen |
|---|---|
| **Fixed payment templates** (always balloon, always even split) | Violates the spec — shape must emerge from the fee objective + creditor flags |
| **Linear / MILP solver** (e.g. OR-Tools) | Correct but heavy for a 5–6 hour take-home; enumeration + simulation is simpler and debuggable |
| **Try k from 1 up** | Fewer payments mean larger early creditor outflows → less room for early fees; max-down aligns better with the objective |
| **Fee-only cadence dates beyond payment dates** | Spec allows it; not needed for provided cases. Would add extra cadence dates after last payment if fee couldn't fit on payment dates alone |
| **Multi-segment DP for staircase** (`max_segments > 2`) | Current code handles 2-level splits; generalizing to S segments via DP would be the production extension |
| **Try multiple lump-sum dates** | Spec says earlier lump is weakly more useful; earliest date suffices for finding minimum L |

---

## Assumptions

1. **`creditor_balance_cents`** on the offer is the creditor balance for
   `offer_total`. Loader also accepts legacy `current_balance_cents`.
2. **Round-half-up** for all monetary rounding (ASSIGNMENT.md §3), implemented
   in `models.round_half_up` — not Python's default banker's rounding.
3. **Program fee dates = payment dates only** for the provided cases. Fee-only
   cadence dates would be added if fee couldn't be fully collected otherwise.
4. **`k` tried from max down** — more cadence dates → more fee front-loading opportunity.
5. **Staircase** enumerates 2-level splits; sufficient for `max_segments ≤ 2` in all cases.

---

## Known limitations

- **Fee-only cadence dates** not generated unless needed (see assumption 3).
- **`max_segments > 2`:** staircase builder uses 2-level splits only; would need
  recursive/DP segmentation for higher caps.
- **Greedy fee collection** takes `min(fee_remaining, balance)` at each date;
  does not reserve balance for future mandatory debits in pathological tight budgets.

---

## Test coverage

| File | Purpose |
|---|---|
| `tests/test_smoke.py` | Loaders, date helpers, serialization |
| `tests/test_cases.py` | Four provided cases — **minimum bar** |
| `tests/test_comprehensive.py` | Rounding, floors, shapes, simulation, guardrails, tiers, token pays |

```bash
pytest tests/test_cases.py -v       # 4 case tests
pytest tests/test_comprehensive.py  # edge cases
pytest -q                           # everything
```
