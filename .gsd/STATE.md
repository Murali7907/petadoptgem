---
updated: 2026-10-13T21:45:00+05:30
---

# Project State: Pet Delivery OTP & Backend Security Hardening

## Current Position

**Milestone:** delivery-otp-security-hardening
**Phase:** Phase 3 - OTP Cryptographic Helpers (Complete) -> Moving to Phase 4 (Delivery Workflow Refactoring & Reissue)
**Status:** executing
**Active Plan:** `.gsd/ROADMAP.md` Phase 4

## Last Action Completed

- **Phase 3 (OTP Cryptographic Helpers)**:
  - Created `backend/apps/logistics/otp.py` implementing `generate_otp()` (cryptographically secure 6 digits), `hash_otp()` (HMAC-SHA256 with `delivery_id:purpose:otp`), `check_otp()` (constant-time digest comparison), and `issue_otp()` (hashes and persists hash only, resets attempts, sets expiries, blanks plaintext, returns code).
  - Integrated `check_otp` into `services.py` for `verify_pickup_otp` and `verify_doorstep_otp`.
  - Aliased `generate_secure_otp` in `services.py` to `generate_otp`.
  - Verified with 4 unit tests in `test_otp_helpers.py` and 14 logistics regression tests passing.

## Next Steps

1. Commit Phase 3 helper module: `feat(logistics): implement cryptographic hmac otp library and constant-time checks`.
2. Begin Phase 4: Delivery workflow changes (decouple doorstep OTP from assignment, issue doorstep OTP on pickup verification, shelter reissue endpoint, strip plaintext OTPs from serializers, restrict `update_status`).

## Active Decisions

| Decision | Choice | Made | Affects |
|---|---|---|---|
| Gitignore Pattern | `/apps` root junction only | 2026-10-13 | Allows `backend/apps/logistics/otp.py` to be tracked by Git |
| Payment Status on Delivery | Do not set `application.payment_status='paid'` unconditionally. Set only if verified online `Payment` exists or COD `cash_collected >= total_amount`; otherwise leave unchanged | 2026-10-13 | Phase 5 verification and terminal state transitions |
| Doorstep OTP Dispatch | Decouple from assignment completely; issue upon shelter pickup only; provide shelter reissue endpoint | 2026-10-13 | Phase 4 workflow and Phase 5 verification |
| Cryptographic Hashing | HMAC-SHA256 keyed by `settings.SECRET_KEY`, payload formatted as `f"{delivery_id}:{purpose}:{otp}"` | 2026-10-13 | Phase 2 schema, Phase 3 OTP helpers |

## Blockers

*None. Planning Lock active.*

## Concerns & Notes

- Dev database has 1 in-flight delivery (`id=3`, `status='out_for_delivery'`) with plaintext OTPs and null expiries. Phase 2 data migration must safely hash and blank this record.
- In-flight testing should use PostgreSQL to guarantee exact behavior of `select_for_update()` and `CheckConstraint`s.
