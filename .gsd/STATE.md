---
updated: 2026-10-13T21:45:00+05:30
---

# Project State: Pet Delivery OTP & Backend Security Hardening

## Current Position

**Milestone:** delivery-otp-security-hardening
**Phase:** Phase 1 - Settings Hardening (Complete) -> Moving to Phase 2 (Schema Migrations)
**Status:** executing
**Active Plan:** `.gsd/ROADMAP.md` Phase 2

## Last Action Completed

- **Phase 1 (Settings Hardening)**: Refactored `backend/config/settings.py` to fail closed when `DEBUG=False` (missing `SECRET_KEY`, `RAZORPAY_*`, or wildcard `*` in `ALLOWED_HOSTS` raises `ImproperlyConfigured`), configured CORS/CSRF reading from env with local defaults, configured `OTP_TTL_MINUTES=1440`, `OTP_MAX_ATTEMPTS=3`, DRF throttle scope `'otp_verify' = '10/min'`, updated `backend/.env.example`, verified media serving with `DEBUG=True`, and passed all 7 settings tests plus 14 logistics regression tests.

## Next Steps

1. Commit Phase 1 settings changes: `fix(settings): fail closed on missing secrets and configure delivery otp throttles`.
2. Begin Phase 2: Add additive fields (`pickup_otp_hash`, `doorstep_otp_hash`, `doorstep_otp_sent_at`), indexes, and check constraints to `PetDelivery`.
3. Create and test reversible forward/backward schema migration on PostgreSQL.
4. Create data migration for existing in-flight delivery #3.

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
