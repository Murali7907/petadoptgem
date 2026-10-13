---
updated: 2026-10-13T22:30:00+05:30
---

# Project State: Pet Delivery OTP & Backend Security Hardening

## Current Position

**Milestone:** delivery-otp-security-hardening
**Phase:** Phase 4 - Delivery Workflow Refactoring & Reissue (Complete) -> Moving to Phase 5 (Verification Endpoints Hardening)
**Status:** executing
**Active Plan:** `.gsd/ROADMAP.md` Phase 5

## Last Action Completed

- **Phase 4 (Delivery Workflow Refactoring & Reissue)**:
  - Refactored `assign_delivery_task`: issues ONLY pickup OTP, attaches single-use `raw_pickup_otp` returned once to shelter in assign response body (`201 Created`), notifies adopter with driver details and NO OTP, creates in-app notification with NO OTP, removes doorstep OTP generation from assignment.
  - Refactored pickup verification: driver pickup OTP verification transitions delivery to `out_for_delivery`, issues doorstep OTP, dispatches email to adopter with confidential verification OTP, sets `doorstep_otp_sent_at`, creates in-app notification without OTP code, logs email errors gracefully without rolling back state transition.
  - Implemented `reissue_otp` endpoint in `ShelterDeliveryViewSet` (`POST /api/v1/shelter/deliveries/<id>/reissue-otp/`): verifies shelter ownership (403), validates purpose `pickup` or `doorstep` (400), enforces state invariants (409 Conflict if pickup reissued outside `assigned`, 409 Conflict if doorstep reissued outside `out_for_delivery`), resets attempt counters and extends expiry TTL, returns plaintext pickup OTP to shelter or emails doorstep OTP to adopter.
  - Stripped plaintext OTP exposure across serializers: removed `pickup_otp` from `DeliveryDriverTaskSerializer`, removed `pickup_otp` and `doorstep_otp` from `ShelterDeliverySerializer` (exposed `doorstep_otp_expires_at` and `doorstep_otp_sent_at`), removed `doorstep_otp` from `DeliveryCustomerSerializer` (exposed `doorstep_otp_sent_at`).
  - Hardened `update_status` transitions: drivers strictly allowed ONLY `status='failed'`, which frees driver (`driver.is_available=True`); rejects `out_for_delivery`, `delivered`, `in_transit`, and arbitrary statuses with HTTP 400 Bad Request.
  - Added direct URL paths for `reissue-otp` (UUID and string IDs, canonical and legacy styles).
  - Verified with 7 integration test suites passing in `scratch/test_phase4_workflow.py`.
  - Committed as `263166c`.

## Next Steps

1. Begin Phase 5: Verification Endpoints Hardening (`select_for_update()`, 6-digit regex validation, `'otp_verify'` throttle scope, attempt tracking with immediate commit, HTTP 400/403/409/410/423 mapping, Pet adopted status, payment status conditional evaluation).
2. Commit Phase 5: `feat(logistics): harden verification endpoints with select_for_update, rate limits, and distinct http errors`.
3. Phase 6: URL routing documentation and verification.
4. Phase 7: Comprehensive test suite and contract documentation.

## Active Decisions

| Decision | Choice | Made | Affects |
|---|---|---|---|
| Gitignore Pattern | `/apps` root junction only | 2026-10-13 | Allows `backend/apps/logistics/otp.py` to be tracked by Git |
| Payment Status on Delivery | Do not set `application.payment_status='paid'` unconditionally. Set only if verified online `Payment` exists or COD `cash_collected >= total_amount`; otherwise leave unchanged | 2026-10-13 | Phase 5 verification and terminal state transitions |
| Doorstep OTP Dispatch | Decouple from assignment completely; issue upon shelter pickup only; provide shelter reissue endpoint | 2026-10-13 | Phase 4 workflow and Phase 5 verification |
| Cryptographic Hashing | HMAC-SHA256 keyed by `settings.SECRET_KEY`, payload formatted as `f"{delivery_id}:{purpose}:{otp}"` | 2026-10-13 | Phase 2 schema, Phase 3 OTP helpers |
| Serializer Hardening | Completely stripped `pickup_otp` and `doorstep_otp` from all serializers | 2026-10-13 | Phase 4 serializer exposure |

## Blockers

*None.*
