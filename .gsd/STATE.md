---
updated: 2026-10-15T12:10:00+05:30
---

# Project State: Milestone 3 — Milestone 2 Gaps, Payment Guards, COD Cash Collection & Adopter Email Integrity

## Current Position

**Milestone:** milestone-3-gaps-and-cod-cash-collection
**Phase:** Complete (Tasks 1, 2, 3, 4 Completed, Tested & Committed)
**Status:** completed
**Active Plan:** Milestone Complete

## Milestone 3 Tasks Completed

- **Task 1: Milestone 2 Gaps & Hardened Authorization Matrix** (`a0a7c9d`):
  - Audited `IsVerifiedShelter` across all shelter write views and viewsets on both canonical and legacy routes (`{"code": "shelter_not_verified"}`).
  - Fixed public pet detail endpoint by public_id/id: returns 404 for pets belonging to unverified shelters when requested by anonymous users or other customers (owner shelter and admin still see it).
  - Expanded authorization matrix in `apps/accounts/test_authorization_matrix.py` covering:
    - Notification read/read-all cross-user boundary isolation (404/403).
    - Application detail GET/PATCH by cross-customer isolation (404/403).
    - Review creation on someone else's application (403).
    - Tracking endpoint access by unrelated user (403).
    - Location ping by unassigned driver (403).
  - Audited Milestone 2 URL aliases: removed unreferenced aliases (`/api/tracking/<id>/`, `/api/v1/tracking/<id>/`, `/api/reviews/create/`, `/api/v1/shelter/drivers/create/`) and repointed all tests and docs to canonical routes.
  - Added standalone tests verifying correctly signed webhooks without `Authorization` header return 200, and invalid signatures return 400.

- **Task 2: Pickup Online Payment Guard** (`054d532`):
  - Documented payment method lifecycle: `apply` defaults to `'unset'`; `assign` sets optional payment method; `create-order` transitions `'unset'`/`'online'` to `'online'`; `'unset'` with `total_amount > 0` blocks pickup until method selected.
  - Implemented `is_payment_satisfied` and `check_pickup_payment_guard` in `apps/logistics/services.py`.
  - Enforced gate at pickup verification: `total_amount == 0` allowed; `cod` allowed; `online` allowed only if a captured payment exists with amount >= total in paise, else 409 `{"code": "payment_required"}`; `unset` with total > 0 returns 409 `{"code": "payment_method_required"}`.
  - Verified failed payment check consumes 0 OTP attempts, and capturing online payment after 409 allows pickup to succeed.
  - Reused `is_payment_satisfied` helper inside `resolve_delivery_payment_status`.

- **Task 3: Cash On Delivery (COD) Collection & Verification** (`4ea2a72`):
  - Created `POST /api/v1/delivery/tasks/<id>/collect-cash/` (and legacy alias `/api/deliveries/<id>/collect-cash/`) with `throttle_scope = 'otp_verify'`.
  - Enforced `select_for_update` under `transaction.atomic()`: validates `out_for_delivery` (409), `payment_method == 'cod'` (400), `total_amount > 0` (400), exact remaining balance check (400 with `remaining_amount`), idempotent return if already collected.
  - On success: updates `cash_collected`, creates exactly one `Payment` ledger row (`method='cod'`, `status='captured'`), and updates `application.payment_status='paid'`.
  - Enforced doorstep payment gate: COD with total > 0 and `cash_collected < total_amount` returns 409 `{"code": "cash_not_collected"}` before OTP verification and consumes 0 attempts.
  - Blocked drivers from marking deliveries as `'failed'` once `cash_collected > 0` (409 with shelter contact instructions). Documented that failed deliveries leave pet in `'pending'` and application in `'approved'`.
  - Added multi-threaded concurrency testing proving at most one Payment row is recorded and doorstep verification gates safely.

- **Task 4: Adopter Email Integrity & Delivery Assignment Safety** (`5fa5d81`):
  - Read-only dev database inspection: found exactly 1 user (`admin123`) with empty email and 0 duplicate emails.
  - Added migration `0006_unique_lower_email_non_empty` containing data-fix step `deduplicate_existing_emails` followed by database `UniqueConstraint(Lower('email'), condition=~Q(email=''), name='unique_lower_email_non_empty')`.
  - Enforced non-empty, valid, case-insensitive unique email in `UserRegistrationSerializer` and `DriverCreateSerializer`.
  - Added gate in `assign_delivery_task`: refuses with 409 `{"code": "adopter_email_missing"}` when adopter has no email.
  - Reissue response detail message accurately reflects outcome: does NOT claim "emailed to adopter" when email dispatch fails (`email_dispatched=False`).
  - Updated API contract documentation in `docs/api_contract_logistics.md`.
  - All test suites passing across all 5 apps.

## Active Decisions

| Decision | Choice | Made | Affects |
|---|---|---|---|
| Pickup Payment Guard | Enforce online capture or cod before pickup OTP verification; consumes 0 attempts | 2026-10-15 | Driver pickup handoff |
| COD Cash Collection | Driver collects cash prior to doorstep OTP check; updates ledger atomically; doorstep verification refuses with 409 `cash_not_collected` if balance remaining | 2026-10-15 | COD doorstep delivery |
| Adopter Email Constraint | Partial unique constraint on `Lower(email)` where `email != ''`; registration requires valid non-empty unique email; assignment refuses without email | 2026-10-16 | User registration & delivery assignment |
| Reissue Outcome Reporting | Response message and `email_dispatched` flag strictly match dispatch outcome without false claims | 2026-10-16 | Reissue OTP API |

## Blockers

*None.*

