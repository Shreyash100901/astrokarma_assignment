# Implementation decisions

The brief leaves several things open. These are the calls I made, why, and what each costs.

## 1. Billing policy: every *started* minute is charged, in advance
`minutes_due = max(1, ceil(elapsed_seconds / 60))`. Minute 1 is charged when the session starts;
minute N is charged as soon as elapsed time passes (N-1) x 60s.
- Matches the brief's example: exactly 3:00 at ₹50/min = ₹150.
- Charging in advance is what makes "end the session when the wallet can't cover the *next* minute"
  well-defined: the user never receives a minute they haven't paid for.
- Cost: a 5-second call costs one full minute. That is standard for per-minute products, but it is a
  policy choice and is a single line (`_bill_locked`) to change.

## 2. Billing is lazy, idempotent and catches up
There is no background worker (out of scope). `bill_session` computes what is owed *as of now* and
bills each missing minute individually, stopping at the first one the wallet can't pay. So a tick
that arrives late, or twice, or concurrently, produces the same result.
`Session.billed_minutes` is the high-water mark. A session therefore only auto-ends when a tick runs;
`bill_active_sessions` is meant to run every few seconds. When a late tick finds an unpayable minute,
`ended_at` and `talk_seconds` are set to the **paid boundary**, not the wall clock, so recorded talk
time can never exceed what the user paid for.

If a user presses "end" after an unpayable minute had already begun, the final billing pass inside
`end_session` finds it and the session is recorded as `system: Insufficient Balance` rather than
`user`. That is deliberate: the system would have ended it at that boundary.

## 3. Per-minute cost comes from the provider, frozen at start
The brief doesn't say where the rate comes from. Each provider has a `per_minute_rate`; `start`
copies it into `Session.per_minute_cost` and billing only ever reads the session's copy. A provider
changing their rate mid-call does not affect a running session (tested).

## 4. Minimum balance
`start` requires `balance >= 5 x rate` (`settings.MIN_BALANCE_MINUTES`) and then immediately charges
minute 1. Exactly 5 x rate is enough to start (tested).

## 5. Money
`Decimal` / `DecimalField(12, 2)` everywhere; no floats. API amounts are strings.

## 6. Idempotency, in layers (a bug in one layer should not become a double charge)
| Concern | App-level guard | DB-level backstop |
|---|---|---|
| Duplicate recharge callback | status check under row lock, plus an atomic `UPDATE ... WHERE status='pending'` claim; only the winner credits | ledger `reference` unique (`recharge:<ref>`) |
| Duplicate minute charge | `billed_minutes` high-water mark, re-read under row lock | `SessionBilling (session, minute_index)` unique; ledger `session:<id>:minute:<n>` unique |
| Negative balance | debit is one `UPDATE ... WHERE balance >= amount` | `CHECK (balance >= 0)` |
| Two active sessions | check under per-user lock | partial unique index on `user WHERE status='in_progress'` |

The credited amount is the one stored at initiation; the callback payload's amount is ignored.
Replays return 200 with `credited: false`. A *conflicting* callback (success after failed, or the
reverse) returns 409 because a terminal state must not change.

## 7. Concurrency
- All wallet changes go through `wallet/services.py`, which refuses to run outside `transaction.atomic()`.
- Lock order is always session -> wallet (recharge: recharge-row -> wallet), so no deadlocks.
- Locking the wallet doubles as a per-user mutex, which is what makes "one active session" race-free.
- SQLite ignores `select_for_update`. On SQLite the conditional UPDATEs and unique constraints still
  prevent corruption, but only PostgreSQL gives true serialisation. The concurrency tests need PostgreSQL.
- Honest note on test strength: removing the row locks makes the parallel-billing test fail (the unique
  constraint stops the double charge but surfaces as an error instead of a clean no-op). The parallel
  callback and parallel start tests still pass without locks, because the atomic-claim UPDATE and the
  partial unique index each cover that case on their own. Those two tests prove the outcome, not that
  the lock specifically is required.

## 8. Structure
Views are thin; the state machine and billing live in `consultations/services.py`. Allowed transitions
are one table (`TRANSITIONS`). Every action locks the session row first and *then* checks its status,
so the status that is validated is the status that is changed. Authorization is checked before state,
so outsiders get 403, not a state error that leaks session details.

## 9. HTTP semantics
Invalid transition, insufficient balance, and a second active session: 400 with `{error, code, ...}`.
Wrong role or not your session: 403. Unknown session or non-participant `GET`: 404. Conflicting
callback: 409. Webhook with a bad secret: 403.

## 10. Smaller calls
- **Who assigns:** the session owner picks the provider (`/assign/`); `create` makes an `unassigned` session.
- **Cancel:** added for pre-start states so sessions don't dangle forever. No provider "reject" (out of scope).
- **Auth:** DRF token auth, custom user model with a `role`, chosen up front because swapping the user model later is painful.
- **Webhook auth:** shared secret header, constant-time compared. A real gateway would use signed payloads.
- **`bill` endpoint** is open to the two participants for convenience; in production it would be internal-only.
- **App name:** `consultations`, because `sessions` collides with `django.contrib.sessions`.

## Known gaps (intentionally out of scope)
No refunds or session disputes; a provider may be in several sessions at once (only the *user* is
limited to one); no token expiry/rotation; no pagination on list endpoints; no real-time auto-end
between ticks (see §2).
