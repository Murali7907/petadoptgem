---
milestone: delivery-otp-security-hardening
version: 1.0.0
updated: 2026-10-13T21:45:00+05:30
---

# Roadmap: Pet Delivery OTP & Backend Security Hardening

> **Current Phase:** Phase 0 - Inspect & Plan (Complete) -> Awaiting Planning Approval
> **Status:** planning (Planning Lock active)

---

## Must-Haves (from SPEC)
- [x] Task 0: Git hygiene fixing `.gitignore` bare `apps` pattern (Verified: `bd5ee5e`)
- [x] Phase 0: Complete inspection of codebase, DB schemas, call sites, and in-flight data (Documented in SPEC.md)
- [x] Phase 1: Settings fail-closed enforcement (`DEBUG=False` default, secret validation, CORS/CSRF, throttle scopes)
- [ ] Phase 2: Additive PostgreSQL schema migration (`pickup_otp_hash`, `doorstep_otp_hash`, `doorstep_otp_sent_at`, indexes, CheckConstraints, data migration)
- [ ] Phase 3: Cryptographic OTP helper library (`apps/logistics/otp.py` with HMAC-SHA256, secrets generation, safe issue/check)
- [ ] Phase 4: Delivery workflow refactoring (assignment OTP decoupled, pickup doorstep issue, shelter reissue endpoint, serializer stripping, `update_status` lock)
- [ ] Phase 5: Verification endpoints hardening (select_for_update, regex validation, HTTP 400/403/409/410/423, Pet adopted status, payment status evaluation)
- [ ] Phase 6: URL routing verification and backward compatibility
- [ ] Phase 7: Comprehensive test suite covering all security constraints and full test suite passing

---

## Phases

### Phase 0: Inspect & Audit
**Status:** ✅ Complete
**Objective:** Perform read-only codebase and database discovery to capture baseline versions, defect confirmations, call sites, constraints, in-flight data, and payment semantics.
**Deliverable:** Phase 0 audit report embedded in `.gsd/SPEC.md`.

---

### Phase 1: Settings Hardening (Fail-Closed)
**Status:** ✅ Complete
**Objective:** Ensure configuration fails closed in production and provides robust local development fallbacks.
**Tasks:**
- [x] Task 1.1: Refactor `backend/config/settings.py` for `DEBUG=False` default, strict `ALLOWED_HOSTS`, `ImproperlyConfigured` for missing secrets when `DEBUG=False`, mock fallbacks only when `DEBUG=True`.
- [x] Task 1.2: Add `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS` env reading with localhost defaults and SimpleJWT explanation comment.
- [x] Task 1.3: Configure `OTP_TTL_MINUTES=1440`, `OTP_MAX_ATTEMPTS=3`, and DRF throttle scope `'otp_verify' = '10/min'`.
- [x] Task 1.4: Update `backend/.env.example` with all newly supported env vars.
- [x] Task 1.5: Verify media serving continues to work when `DEBUG=True`.
**Required Proof:** `python manage.py check`, test missing `SECRET_KEY` with `DEBUG=False` raises `ImproperlyConfigured`.

---

### Phase 2: Schema Migrations (Additive, Reversible PostgreSQL)
**Status:** ⬜ Not Started
**Objective:** Add cryptographic hash fields, indexes, and DB check constraints, followed by data migration of in-flight deliveries.
**Tasks:**
- [ ] Task 2.1: Update `PetDelivery` model in `apps/logistics/models.py` with `pickup_otp_hash`, `doorstep_otp_hash`, `doorstep_otp_sent_at`, indexes `(status)` and `(driver, status)`, and `CheckConstraint`s.
- [ ] Task 2.2: Generate schema migration and review SQL.
- [ ] Task 2.3: Author data migration hashing existing in-flight deliveries (`status not in ('delivered', 'failed')`), blanking plaintext, and setting missing expiries. Document reverse as safe no-op.
- [ ] Task 2.4: Execute `migrate` forward and backward against PostgreSQL and capture logs.
**Required Proof:** Postgres migration logs forward and backward, `\d pet_deliveries` showing new columns and constraints.

---

### Phase 3: OTP Cryptographic Helpers
**Status:** ⬜ Not Started
**Objective:** Create isolated, secure cryptographic OTP library without plaintext storage.
**Tasks:**
- [ ] Task 3.1: Create `backend/apps/logistics/otp.py` implementing `generate_otp()`, `hash_otp()`, `check_otp()`, and `issue_otp()`.
- [ ] Task 3.2: Remove plaintext OTP auto-generation from `PetDelivery.save()`.
- [ ] Task 3.3: Unit test `otp.py` functions (HMAC validation, constant-time compare, salt/delivery separation).
**Required Proof:** Unit test execution and assertion output.

---

### Phase 4: Delivery Workflow Refactoring & Reissue
**Status:** ⬜ Not Started
**Objective:** Align lifecycle transitions so doorstep OTP is issued only upon shelter pickup, remove plaintext leakage from serializers, and introduce shelter reissue.
**Tasks:**
- [ ] Task 4.1: Refactor `assign_delivery_task` to issue only pickup OTP, notify adopter without OTP, and return plaintext pickup OTP once to shelter.
- [ ] Task 4.2: Refactor pickup transition to issue doorstep OTP, send email to customer with sent timestamp, and create in-app notification without OTP text.
- [ ] Task 4.3: Implement `reissue_otp` endpoint in `ShelterDeliveryViewSet` for pickup and doorstep purposes.
- [ ] Task 4.4: Strip `pickup_otp` and `doorstep_otp` from `DeliveryDriverTaskSerializer`, `ShelterDeliverySerializer`, and `DeliveryCustomerSerializer`.
- [ ] Task 4.5: Enforce strict `update_status` transitions (allow only `failed`, reject arbitrary strings and skip-transitions).
**Required Proof:** API endpoint tests asserting serializer fields, reissue flow, and transition barriers.

---

### Phase 5: Verification Endpoints Hardening
**Status:** ⬜ Not Started
**Objective:** Secure verification endpoints with concurrency locking, rate limiting, and distinct status codes.
**Tasks:**
- [ ] Task 5.1: Harden `pickup` view with `select_for_update()`, 6-digit regex validation, `'otp_verify'` throttle scope, and attempt tracking.
- [ ] Task 5.2: Harden `verify_doorstep` view with `select_for_update()`, 6-digit regex validation, `'otp_verify'` throttle scope, attempt tracking, and idempotent 200 for already delivered tasks.
- [ ] Task 5.3: Implement distinct HTTP error mapping: 400 (bad input / wrong code with attempts left), 403 (unauthorized driver), 409 (invalid state), 410 (expired), 423 (locked out after max attempts).
- [ ] Task 5.4: Implement terminal actions on doorstep success: `pet.status = 'adopted'`, `application.status = 'completed'`, conditional payment status evaluation (online verified payment or COD collected).
**Required Proof:** Integration test suite asserting each HTTP status code and state mutation.

---

### Phase 6: URL Routing & Backward Compatibility
**Status:** ⬜ Not Started
**Objective:** Maintain canonical `/api/v1/` routes and legacy aliases without breaking clients.
**Tasks:**
- [ ] Task 6.1: Verify `apps/logistics/urls.py` and project URLs for both canonical and legacy routes.
- [ ] Task 6.2: Add comments designating legacy routes.
**Required Proof:** URL reverse tests and drf-spectacular schema validation.

---

### Phase 7: Comprehensive Test Suite & Verification Proof
**Status:** ⬜ Not Started
**Objective:** Author end-to-end tests validating all requirements and run the full test suite.
**Tasks:**
- [ ] Task 7.1: Author comprehensive tests in `backend/apps/logistics/tests.py` covering all positive and negative security cases.
- [ ] Task 7.2: Update legacy tests where behavior intentionally changed (documenting reason for each change).
- [ ] Task 7.3: Create contract documentation `docs/api_contract_logistics.md`.
- [ ] Task 7.4: Execute `python manage.py test apps.logistics` and full `python manage.py test`.
- [ ] Task 7.5: Execute `python manage.py check` and `git status` verifying no frontend files touched.
**Required Proof:** Captured output of all test runs and checks.

---

## Progress Summary

| Phase | Description | Status | Tasks Complete |
|---|---|---|---|
| Phase 0 | Inspect & Audit | ✅ Complete | 1/1 |
| Phase 1 | Settings Hardening | ⬜ Not Started | 0/5 |
| Phase 2 | Schema Migrations | ⬜ Not Started | 0/4 |
| Phase 3 | OTP Cryptographic Helpers | ⬜ Not Started | 0/3 |
| Phase 4 | Workflow Refactoring & Reissue | ⬜ Not Started | 0/5 |
| Phase 5 | Verify Endpoints Hardening | ⬜ Not Started | 0/4 |
| Phase 6 | URL Routing | ⬜ Not Started | 0/2 |
| Phase 7 | Tests & Verification Proof | ⬜ Not Started | 0/5 |
