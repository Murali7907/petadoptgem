---
updated: 2026-10-15T12:10:00+05:30
---

# Project State: Milestone 2 — Payment Hardening, Verified Shelters & Authorization Matrix

## Current Position

**Milestone:** milestone-2-hardening-and-authorization
**Phase:** Complete (Tasks 1, 2, 3 Completed & Tested)
**Status:** completed
**Active Plan:** Milestone Complete

## Milestone 2 Tasks Completed

- **Task 1: Payment Hardening** (`be4524d`):
  - Refactored `create-order`: enforces ownership (404), ignores client amount, derives paise with Decimal from `application.total_amount`, validates preconditions (total > 0, status approved, online/unset method, status not paid, delivery payable), locks row atomically, and reuses existing unpaid orders.
  - Razorpay order creation executed before row locks to eliminate transaction lock contention.
  - Hardened `verify`: locks row, enforces applicant ownership (404), verifies HMAC signature, checks amount safety, idempotent return for already captured payments.
  - Hardened `webhook`: AllowAny permission with empty authentication classes, verifies signature over raw body, compares amount & currency, never leaks internal stack traces.
  - Centralized `mark_payment_captured` atomic state machine.

- **Task 2: Verified Shelters Enforcement** (`1112867`):
  - Created `IsVerifiedShelter` permission with admin bypass and custom 403 response (`{"code": "shelter_not_verified", "detail": "..."}`).
  - Applied to `ShelterDriverCreateView`, `ShelterPetViewSet`, `PetViewSet`, `PetImageCreateView`, `PetMedicalRecordListCreateView`, `ShelterApplicationViewSet` (`review`, `approve`, `reject`), `AdoptionStatusUpdateView`, and `ShelterDeliveryViewSet` (`assign`, `reissue_otp`).
  - Filtered public pet catalogs (`PetViewSet.list`, `PublicPetViewSet.list`) by `shelter__is_verified=True`.
  - Updated all fixtures and tests across all apps.

- **Task 3: Authorization Matrix Tests & Gap Resolution** (`e823382`):
  - Created comprehensive test suite `apps/accounts/test_authorization_matrix.py` covering all 12 platform operations and cross-tenant/role matrices.
  - Added route aliases: `/api/v1/shelter/drivers/create/`, direct `/api/tracking/<id>/`, `/api/v1/tracking/<id>/`, and `/api/reviews/create/`.
  - Fixed `ShelterDeliveryViewSet.get_object` to call `check_object_permissions(self.request, obj)`.
  - Fixed `DriverDeliveryViewSet.get_permissions` to enforce `IsDeliveryParticipant` on all direct tracking actions.
  - Full test suite: **126 tests passing across all 5 apps (0 failures, 0 errors)**.

## Active Decisions

| Decision | Choice | Made | Affects |
|---|---|---|---|
| Payment Status on Delivery | Do not set `application.payment_status='paid'` unconditionally. Set only if verified online `Payment` exists or COD `cash_collected >= total_amount`; otherwise leave unchanged | 2026-10-13 | Delivery verification |
| Verified Shelter Gate | Enforce `IsVerifiedShelter` on all shelter write endpoints; return 403 with `code: shelter_not_verified` | 2026-10-15 | All shelter write endpoints & public pet catalogs |
| Direct Tracking Security | Enforce `IsDeliveryParticipant` in `DriverDeliveryViewSet.get_permissions` for `tracking` and `track` actions | 2026-10-15 | Real-time tracking security |

## Blockers

*None.*
