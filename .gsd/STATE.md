---
updated: 2026-10-19T17:35:00+05:30
---

# Project State: Milestone 4 — Production Hardening, Validation, API Standards & Operational Hygiene

## Current Position

**Milestone:** milestone-4-hardening-and-operational-hygiene
**Phase:** Complete (All Tasks 1 through 8 Completed, Verified & Committed)
**Status:** completed
**Active Plan:** Milestone Complete

## Milestone 4 Tasks Completed

- **Task 1: Review 500 Guard** (`937a3e9`):
  - Pre-insert validation in `ReviewCreateSerializer` and `ReviewCreateAPIView`.
  - Catches database-level trigger exceptions (`trg_check_review_eligibility`) gracefully, mapping them to 400 clean response.
  - Tests verify: non-delivered delivery (400), non-existent delivery (400), duplicate review (400), foreign applicant (403).
  - App test summary: `Ran 5 tests in 14.945s. OK.`

- **Task 2: Shelter Cancel Delivery** (`1421ad0`):
  - Added `POST /api/v1/shelter/deliveries/<id>/cancel/` with `throttle_scope = 'shelter_cancel'`.
  - Restricted to verified owning shelter (`IsVerifiedShelter`) or platform admin.
  - Invariants: allowed strictly from `assigned` or `out_for_delivery` AND `cash_collected == 0`.
  - Sets delivery to `failed`, frees the driver, retains pet in `pending` and application in `approved`, sends adopter notification on transaction commit.
  - Blocks cancellation if `cash_collected > 0` with 409 `{"code": "cash_collected"}`. Idempotent for already-failed deliveries.
  - Documented in `docs/api_contract_logistics.md`.
  - App test summary: `Ran 88 tests in 223.518s. OK.`

- **Task 3: Auth Abuse Protection** (`1ade1f4`):
  - Added rate throttling for `auth_login` (10/min), `auth_register` (5/min), and `auth_refresh` (30/min), overridable via env.
  - Added `POST /api/v1/auth/logout/` blacklisting refresh tokens; idempotent for blacklisted tokens (200), rejects invalid tokens (400).
  - App test summary: `Ran 44 tests in 134.821s. OK.`

- **Task 4: Upload Validation** (`0c47615`):
  - Validates pet image binary signatures via Pillow `Image.verify()` and format matching (`.jpg`, `.jpeg`, `.png`, `.webp`), 10 MB limit.
  - Validates medical document content signatures (`%PDF-` for PDFs, Pillow for images), 15 MB limit.
  - Rejects renamed executables/text files with 400. Enforces `default_storage` only.
  - App test summary: `Ran 34 tests in 47.787s. OK.`

- **Task 5: Settings and Dependencies** (`825996d`):
  - `CACHES`: Redis when `REDIS_URL` is set; LocMemCache when `DEBUG=True`; startup warning when `DEBUG=False` without Redis.
  - `LOGGING`: Console handler with level from `LOG_LEVEL` (default `INFO`) and `MaskSensitiveDataFilter`/`MaskSensitiveFormatter` redacting OTPs, tokens, and secrets.
  - Production security headers when `DEBUG=False`: `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_CONTENT_TYPE_NOSNIFF`, `SECURE_REFERRER_POLICY`, optional `SECURE_PROXY_SSL_HEADER`.
  - Pinned dependencies to `requirements.lock.txt` (UTF-8 without BOM).
  - Updated `backend/.env.example` with all new variables.
  - Deploy check achieves 0 security warnings (`Ran 46 tests in 151.980s. OK.`).

- **Task 6: OpenAPI and Routes** (`5d235a9`):
  - Resolved 4 schema errors in `apps/adoptions/views.py` with `@extend_schema`.
  - Implemented `SPECTACULAR_SETTINGS['PREPROCESSING_HOOKS']` in `backend/config/openapi.py` filtering to canonical `/api/v1/` routes.
  - Eliminated all operationId collisions (before: 26+, after: 0). Schema errors: 0.
  - App test summary: `Ran 14 tests in 79.614s. OK.`

- **Task 7: Stale Schema File and Trigger Audit** (`49aadf2`):
  - Audited `pg_trigger` in PostgreSQL dev database: `trg_sync_pet_status` exists on `adoption_applications` calling `sync_pet_status_on_application_change()`.
  - Relocated `backend/schema.sql` to `docs/legacy/schema.sql.old` with deprecation disclaimer.
  - Documented trigger lifecycle behavior in `.gsd/SPEC.md`.

- **Task 8: Medical Records Visibility Security Options (Report Only)**:
  - Documented three visibility architectures (Public, Logged-in only, Hybrid) and their respective test suites in `.gsd/SPEC.md` and final report.

## Active Decisions

| Decision | Choice | Made | Affects |
|---|---|---|---|
| Review 500 Guard | Explicit application & delivery status check before review insert | 2026-10-17 | Review submission |
| Shelter Delivery Cancellation | Allowed from assigned/out_for_delivery with cash_collected == 0; frees driver; leaves pet pending | 2026-10-17 | Logistics delivery management |
| Auth Throttling & Logout | Scoped rate limits per endpoint; token blacklist on logout | 2026-10-17 | Auth endpoints |
| Upload Validation | Binary magic byte verification (Pillow / PDF header) ignoring Content-Type | 2026-10-17 | Pet image & medical uploads |
| OpenAPI Schema Cleaning | drf-spectacular preprocessing hook keeps canonical /api/v1/ endpoints, resolving collisions | 2026-10-19 | API documentation & OpenAPI schema |

## Blockers

*None.*
