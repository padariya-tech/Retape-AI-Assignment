# Settlement Feasibility & Fee Engine

## Overview

This solution evaluates whether a settlement offer can be completed using the client's escrow account (SDA) while satisfying all creditor rules.

The engine performs two tasks:

1. Determine whether a valid settlement schedule exists.
2. If not, determine:

   * Minimum lump-sum funding required.
   * Minimum monthly draft increase required.

The implementation prioritizes the business objective stated in the assignment:

> Collect program fees as early as possible while respecting creditor constraints.

---

# High Level Approach

The solution consists of four major steps:

```text
Offer
  │
  ▼
Generate Candidate Payment Schedule
  │
  ▼
Simulate SDA Ledger
  │
  ▼
Feasible ?
 ├── Yes → Return Schedule
 └── No  → Compute Additional Funds
```

---

# Step 1: Calculate Required Amounts

## Offer Total

Amount that must be paid to the creditor:

```text
offer_total =
round(settlement_pct × creditor_balance)
```

Example:

```text
Creditor Balance = $1000
Settlement %     = 50%

Offer Total = $500
```

---

## Program Fee

Amount collected by the company:

```text
program_fee =
round(program_fee_pct × original_balance)
```

Example:

```text
Original Balance = $1200
Program Fee %    = 25%

Program Fee = $300
```

---

# Step 2: Generate Creditor Payments

The engine generates creditor payment schedules based on creditor flags.

---

## Even Schedule

Used when:

```text
even_pays = true
```

All payments are equal (or as equal as possible).

Example:

```text
Offer Total = 50000
Payments    = 6

8333
8333
8333
8333
8334
8334
```

Remainder cents are placed on the latest payments to maintain a non-decreasing sequence.

---

## Balloon Schedule

Used when:

```text
is_ballooning_allowed = true
```

Early payments are kept at their minimum legal values and the final payment absorbs the remaining balance.

Example:

```text
2500
2500
2500
2500
2500
37500
```

This naturally supports early fee collection.

---

## Staircase Schedule

Used when:

```text
even_pays = false
is_ballooning_allowed = false
```

The implementation generates a two-level staircase.

Example:

```text
2500
2500
2500
14167
14167
14166
```

The first segment uses minimum legal payments.

The remaining settlement amount is distributed evenly across later payments.

This interpretation was chosen because it maximizes early fee collection while respecting creditor constraints.

The implementation treats `last_draft_date` as the scheduling horizon. Creditor payments are restricted to occur on or before this date, ensuring all scheduled payments are supported by known future funding events.
---

# Step 3: Simulate the Ledger

After generating creditor payments, the engine performs a full ledger simulation.

All future account activity is processed chronologically.

```text
Credits
  ↓
Debits
  ↓
Creditor Payments
  ↓
Bank Fees
  ↓
Program Fees
```

Same-day processing follows:

```text
Credits before Debits
```

as required by the assignment.

---

## Simulation Flow

```text
Start Balance
      │
      ▼
Apply Credits
      │
      ▼
Apply Existing Debits
      │
      ▼
Apply Creditor Payment
      │
      ▼
Apply Bank Fee
      │
      ▼
Collect Program Fee
      │
      ▼
Record Balance
```

---

# Program Fee Collection Strategy

The assignment objective is:

> Collect program fees as early as possible.

Therefore the implementation uses a greedy strategy.

At each cadence date:

```text
1. Pay creditor
2. Pay bank fee
3. Collect as much fee as possible
```

Example:

```text
Balance after obligations = $165

Fee Remaining = $300

Collect $165
```

This continues until:

```text
Fee fully collected
```

or

```text
No funds available
```

---

# Step 4: Feasibility Check

A schedule is feasible when:

```text
1. No balance becomes negative.
2. Program fee is fully collected.
3. Offer total is fully paid.
4. All creditor rules are satisfied.
```

```text
Running Balance

200
150
75
0
25
```

Feasible

```text
Running Balance

200
150
-1
```

Infeasible

---

# Additional Funds Calculation

When no feasible schedule exists, the engine computes two independent solutions.

---

## Lump Sum

Question:

```text
How much money must be added once?
```

The lump sum is placed on the earliest future credit date.

Binary search is used to find the minimum amount.

```text
0      -> Fail
10000  -> Works
5000   -> Fail
7500   -> Works
...
```

Result:

```text
Minimum Lump Sum
```

---

## Monthly Increment

Question:

```text
How much should every future draft increase?
```

Example:

```text
Current Draft = $200

Increase = $25

New Draft = $225
```

Binary search is used to find the minimum required increase.

Result:

```text
Minimum Monthly Increment
```

---

# Guardrails

The assignment requires reporting whether additional funding exceeds recommended limits.

---

## Lump Sum Guardrail

```text
65% of Offer Total
```

Example:

```text
Offer Total = $500

Limit = $325
```

---

## Monthly Increment Guardrail

```text
max(
    $100,
    40% of Draft Amount
)
```

Example:

```text
Draft = $200

40% = $80

Limit = $100
```

---

# Assumptions & Interpretations

## Staircase Interpretation

The assignment intentionally leaves staircase construction open-ended.

This implementation uses:

```text
Early Floor Payments
        +
Evenly Distributed Higher Payments
```

which produces a two-level staircase.

---

## Segment Counting

Small ±1 cent differences caused by integer division are ignored.

Example:

```text
2500
2500
14166
14167
14167
```

is treated as:

```text
2 Segments
```

not 3.

---

## Lump Sum Placement

Lump sums are added to the earliest available future credit date because earlier funds are always at least as useful as later funds.

---

## Monthly Increment

The same increment amount is applied uniformly to every future draft.

---

# Complexity

Let:

```text
P = maximum payment count
M = search space for additional funds
```

Schedule generation:

```text
O(P²)
```

Simulation:

```text
O(number of ledger dates)
```

Additional funds search:

```text
O(log M × schedule_generation)
```

using binary search.

---

# Testing

The solution was tested against:

* Even payment schedules.
* Balloon schedules.
* Staircase schedules.
* Token payment limits.
* Tiered minimum payments.
* Exact settlement totals.
* Fee collection rules.
* Horizon limits.
* Lump sum calculations.
* Monthly increment calculations.
* Ledger balance feasibility.
