---
updated: 2026-10-13T21:45:00+05:30
---

# Project State: Pet Delivery OTP & Backend Security Hardening

## Current Position

**Milestone:** delivery-otp-security-hardening
**Phase:** Phase 2 - Schema Migrations (Complete) -> Moving to Phase 3 (OTP Cryptographic Helpers)
**Status:** executing
**Active Plan:** `.gsd/ROADMAP.md` Phase 3

## Last Action Completed

- **Phase 2 (Schema Migrations)**:
  - Created pg_dump backup `pet_adoption_pre_migration.sql` (79,813 bytes).
  - Added `pickup_otp_hash`, `doorstep_otp_hash`, `doorstep_otp_sent_at` to `PetDelivery`, along with indexes `(status)` and `(driver, status)`, and check constraints for non-negative attempts and `status='delivered' => delivered_at IS NOT NULL`.
  - Removed auto-generation of plaintext OTPs from `PetDelivery.save()`.
  - Created additive reversible migrations `0004_petdelivery_doorstep_otp_hash_and_more.py` and data migration `0005_hash_inflight_otps.py`.
  - Successfully executed migrations backward to `0003` and forward to `0005` on PostgreSQL.
  - Verified that in-flight delivery #3 was successfully hashed, plaintext columns blanked, and expiries set.

## Next Steps

1. Commit Phase 2 schema and data migrations: `feat(logistics): add otp hash columns, constraints, and data migration for in-flight deliveries`.
2. Begin Phase 3: Create `apps/logistics/otp.py` implementing `generate_otp()`, `hash_otp()`, `check_otp()`, and `issue_otp()`.
3. Unit test `otp.py` and remove legacy `generate_secure_otp` references.

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
