# SPEC.md — Pet Delivery OTP & Backend Security Hardening Specification

> **Status**: `FINALIZED`
>
> ⚠️ **Planning Lock**: Spec approved and finalized.

---

## 1. Vision & Background
The platform is an existing Django 6.1.2 / Django REST Framework 3.18.3 pet adoption and delivery service running on PostgreSQL with SimpleJWT authentication. This specification defines a backend and database security refactoring for the pet delivery workflow, replacing insecure plaintext OTP storage and flawed lifecycle transitions with HMAC-SHA256 hashed one-time passwords, atomic race-condition protected verifications, fail-closed production settings, and automated cryptographic OTP issuance.

---

## 2. Phase 0 — Inspection Findings (Audit Report)

### 2.1 Django and DRF Versions
- **Django**: `6.1.2`
- **Django REST Framework**: `3.18.3`
- **Database Engine**: PostgreSQL (`pet_adoption`, port 5432)

### 2.2 Pre-existing Defect Confirmations
All 8 defects reported in the prompt were inspected in the codebase and confirmed:

1. **Defect 1: Status Transition Bypass in `update_status`**:
   - Confirmed in `backend/apps/logistics/views.py` (`DeliveryDriverTaskViewSet.update_status`, lines 205–223).
   - Allows a driver to PATCH `status="out_for_delivery"` from `assigned`, bypassing shelter pickup OTP verification. Accepts arbitrary status strings (e.g., `"in_transit"`, `"random_string"`).
2. **Defect 2: Plaintext OTP Exposure in Serializers**:
   - Confirmed in `backend/apps/logistics/serializers.py`:
     - `DeliveryDriverTaskSerializer` (line 50) includes `pickup_otp` in exposed fields, allowing the driver to read the shelter's secret code.
     - `ShelterDeliverySerializer` (lines 199–200) exposes both `pickup_otp` and `doorstep_otp` to the shelter.
     - `DeliveryCustomerSerializer` (line 145) exposes `doorstep_otp` to the customer via tracking endpoints.
3. **Defect 3: Doorstep OTP Premature Dispatch & Plaintext Leaks**:
   - Confirmed in `backend/apps/logistics/views.py` and `services.py`:
     - `dispatch_doorstep_otp` is called at assignment (`assign_driver`, line 117), pickup (`pickup`, line 196), and status update (`update_status`, line 226).
     - Customer receives doorstep verification OTP via email before the pet has even been picked up from the shelter.
     - `services.py` (lines 232–237) creates an in-app `Notification` containing the plaintext OTP in the message body: `"Your 6-digit verification OTP is {secret_otp}"`.
4. **Defect 4: Plaintext Storage, Hardcoded 5-Attempt Limit, Missing Locks & Throttling**:
   - Confirmed in `backend/apps/logistics/services.py`:
     - Plaintext equality comparison: `str(submitted_otp).strip() == str(delivery.doorstep_otp).strip()`.
     - Hardcoded limit of 5 attempts (`delivery.pickup_otp_attempts >= 5`, `delivery.doorstep_otp_attempts >= 5`).
     - No `select_for_update()` on verification queries.
     - All verification errors raise `rest_framework.exceptions.ValidationError` resulting in uniform HTTP 400 responses.
     - No DRF view throttling scope on verification actions.
5. **Defect 5: Missing Reissue Mechanism**:
   - Confirmed: No reissue endpoint exists. If an OTP expires or is locked out after failed attempts, the delivery is permanently stuck, and the driver remains unavailable (`driver.is_available = False`).
6. **Defect 6: Premature Expiry Timestamp Initialization**:
   - Confirmed in `backend/apps/logistics/services.py` (`assign_delivery_task`, line 63, 76): `doorstep_otp_expires_at` is set at shelter assignment time (12 hours) instead of when the delivery actually transitions to `out_for_delivery`.
   - `PetDelivery.save()` in `backend/apps/logistics/models.py` (lines 47–54) auto-generates plaintext `pickup_otp`, `doorstep_otp`, and `secret_otp` on every model save if empty.
7. **Defect 7: Unconditional Payment Status Mutation & Missing Pet Status Update**:
   - Confirmed in `backend/apps/logistics/services.py` (`verify_doorstep_otp`, lines 145–149):
     - `application.payment_status = 'paid'` is set unconditionally regardless of whether online payment succeeded or COD was collected.
     - `pet = application.pet` is never updated to `status = 'adopted'`.
8. **Defect 8: Permissive Settings Defaults**:
   - Confirmed in `backend/config/settings.py`:
     - `DEBUG` defaults to `True` (line 15).
     - `ALLOWED_HOSTS` defaults to `'*'` (line 18).
     - `SECRET_KEY` falls back to `'django-insecure-pet-adoption-secret-key'` (line 12).
     - `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` fall back to mock strings unconditionally without checking `DEBUG`.
     - `CORS_ALLOWED_ORIGINS` hardcoded to localhost (lines 229–232); `CSRF_TRUSTED_ORIGINS` absent.

### 2.3 Call Sites of `dispatch_doorstep_otp`
- `backend/apps/logistics/views.py`:
  - Line 117: inside `ShelterDeliveryViewSet.assign_driver`
  - Line 196: inside `DeliveryDriverTaskViewSet.pickup`
  - Line 226: inside `DeliveryDriverTaskViewSet.update_status`
- `backend/apps/logistics/services.py`:
  - Line 153: function definition
- `backend/apps/logistics/tests.py`:
  - Lines 409, 426, 444, 460, 476 (legacy tests asserting old dispatch behavior)

### 2.4 Existing Logistics Tests Asserting OTP Behavior
- `test_driver_assignment_and_availability_toggle` (lines 116–117): asserts 6-digit plaintext OTPs generated on assignment.
- `test_pickup_otp_verification` (line 173): submits `delivery.pickup_otp`.
- `test_doorstep_otp_brute_force_protection` (lines 180–204): asserts 5-attempt limit returns 400.
- `test_successful_doorstep_otp_verification` (lines 205–225): asserts doorstep completion.
- `test_dispatch_doorstep_otp_*` (lines 409–494): 5 tests asserting OTP dispatch email content and triggers.

### 2.5 Pet and Application Status Transitions
- `application.status`:
  - `adoptions/views.py`: line 132 set to `'approved'` on shelter approval.
  - `adoptions/views.py`: line 166, 377 status validation.
  - `logistics/services.py`: line 146 set to `'completed'` upon doorstep delivery.
- `Pet.status`:
  - `adoptions/views.py`: line 138 set to `'pending'` on shelter approval.
  - `backend/schema.sql` (line 261): Trigger had `UPDATE pets SET status = 'adopted' WHERE id = NEW.pet_id`, but in Django application code `pet.status = 'adopted'` was **never set**.

### 2.6 Existing Indexes and Constraints on `pet_deliveries`
- **Postgres Indexes**:
  - `pet_deliveries_pkey`: UNIQUE btree (`id`)
  - `pet_deliveries_public_id_key`: UNIQUE btree (`public_id`)
  - `pet_deliveries_application_id_key`: UNIQUE btree (`application_id`)
  - No index on `(status)` or `(driver, status)`.
- **Postgres Constraints**:
  - Primary key, foreign keys, not-null constraints.
  - Zero check constraints.
- **Data Validation**:
  - Existing DB rows satisfy `attempts >= 0` and `status = 'delivered' implies delivered_at IS NOT NULL`.

### 2.7 In-Flight Deliveries in Dev Database
- **Total Deliveries in PostgreSQL**: 2
  - Delivery ID 4: `status = 'delivered'`, `delivered_at = 2026-10-05 18:02:06 UTC`.
  - Delivery ID 3: `status = 'out_for_delivery'`, `pickup_otp = '582910'`, `doorstep_otp = '419283'`, `secret_otp = '419283'`, `expires_at = None`, `doorstep_otp_expires_at = None`.
- **Confirmation**: Dev data contains 1 active in-flight delivery holding plaintext OTPs and missing expiries. The data migration in Phase 2 must hash these values, blank the plaintext fields, and populate sensible expiries.

### 2.8 Payment Representation (COD vs Online)
- `AdoptionApplication.payment_method`: `'unset'`, `'online'`, `'cod'`
- `AdoptionApplication.payment_status`: `'pending'`, `'paid'`, `'failed'`
- `Payment` model: `amount`, `payment_method`, `status` (`'created'`, `'paid'`, `'failed'`), `razorpay_payment_id`
- `PetDelivery`: `cash_collected` (Decimal), `is_cash_settled_with_shelter` (Boolean)
- **Phase 5 Payment Decision**: When doorstep OTP is verified:
  - If `application.payment_method == 'online'`: check if a verified `Payment` row with `status='paid'` exists. If yes, set `application.payment_status = 'paid'`.
  - If `application.payment_method == 'cod'`: check if `delivery.cash_collected >= application.total_amount` (and total > 0). If yes, set `application.payment_status = 'paid'`.
  - Otherwise, leave `application.payment_status` unchanged (e.g. `'pending'`).

---

## 3. Goals & Requirements by Phase

### Phase 1 — Settings Hardening (Fail-Closed)
1. `DEBUG`: defaults to `False`. Local dev `.env` and `backend/.env.example` explicitly set `DEBUG=True`.
2. `ALLOWED_HOSTS`: read from env. When `DEBUG=False`, wildcard `*` is forbidden and raises `ImproperlyConfigured` or requires explicit hosts. When `DEBUG=True`, defaults to `['localhost', '127.0.0.1']`.
3. Secrets: `SECRET_KEY`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` must raise `django.core.exceptions.ImproperlyConfigured` if missing or empty when `DEBUG=False`. Mock fallbacks allowed only when `DEBUG=True`.
4. CORS & CSRF: `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS` read from env as comma-separated lists, defaulting to `http://localhost:3000,http://127.0.0.1:3000` in dev. Include code comment noting that authentication is JWT-only (stateless SimpleJWT headers) so CSRF cookies do not apply to DRF API endpoints.
5. Rate Limiting & TTL:
   - `OTP_TTL_MINUTES`: default `1440` (24 hours).
   - `OTP_MAX_ATTEMPTS`: default `3`.
   - DRF Throttle: Add `'otp_verify': os.getenv('OTP_THROTTLE_RATE', '10/min')` to `DEFAULT_THROTTLE_RATES`.
6. Confirm local media serving still works with `DEBUG=True`.

### Phase 2 — Schema Migrations (Additive, Reversible PostgreSQL)
1. Additive fields on `PetDelivery`:
   - `pickup_otp_hash = models.CharField(max_length=64, null=True, blank=True)`
   - `doorstep_otp_hash = models.CharField(max_length=64, null=True, blank=True)`
   - `doorstep_otp_sent_at = models.DateTimeField(null=True, blank=True)`
2. Preserve legacy fields: `pickup_otp`, `doorstep_otp`, `secret_otp` remain in schema (`blank=True, default=''`), never dropped or renamed.
3. Indexes:
   - Index on `(status)`
   - Index on `(driver, status)`
4. CheckConstraints:
   - `check_pickup_otp_attempts_gte_0`: `pickup_otp_attempts >= 0`
   - `check_doorstep_otp_attempts_gte_0`: `doorstep_otp_attempts >= 0`
   - `check_delivered_has_delivered_at`: `Q(status='delivered', delivered_at__isnull=False) | ~Q(status='delivered')`
5. Data Migration:
   - Forward: inspect existing deliveries with status not in `('delivered', 'failed')` having non-empty plaintext OTPs. Hash them using `hash_otp(delivery.id, purpose, otp)`, populate `pickup_otp_hash` and `doorstep_otp_hash`, blank the plaintext fields, and set missing `expires_at` / `doorstep_otp_expires_at` to `now + OTP_TTL_MINUTES`.
   - Backward: reversible no-op with documented explanation.
6. Verify forward and backward migration on PostgreSQL.

### Phase 3 — OTP Cryptographic Helpers (`backend/apps/logistics/otp.py`)
1. `generate_otp() -> str`: Generate cryptographically secure 6-digit decimal string (`secrets.choice(string.digits)` or `secrets.randbelow(900000) + 100000`).
2. `hash_otp(delivery_id: int | str, purpose: str, otp: str) -> str`: HMAC-SHA256 keyed by `settings.SECRET_KEY.encode('utf-8')`. Message formatted as `f"{delivery_id}:{purpose}:{otp}".encode('utf-8')`. Returns 64-character hex digest.
3. `check_otp(stored_hash: str | None, delivery_id: int | str, purpose: str, submitted: str) -> bool`: Computes HMAC-SHA256 and uses `hmac.compare_digest(stored_hash, computed_hash)`.
4. `issue_otp(delivery: PetDelivery, purpose: str) -> str`:
   - Validates purpose in `('pickup', 'doorstep')`.
   - Generates 6-digit OTP plaintext.
   - Computes hash and saves to `pickup_otp_hash` or `doorstep_otp_hash`.
   - Sets `pickup_otp_attempts = 0` or `doorstep_otp_attempts = 0`.
   - Sets `expires_at` or `doorstep_otp_expires_at` to `now + timedelta(minutes=settings.OTP_TTL_MINUTES)`.
   - Blanks plaintext legacy columns (`pickup_otp = ''` or `doorstep_otp = ''`, `secret_otp = ''`).
   - Persists changes without logging or printing plaintext.
   - Returns plaintext string to caller for single-use dispatch.
5. Clean up `PetDelivery.save()`: Remove auto-generation of plaintext OTPs.

### Phase 4 — Delivery Workflow Refactoring & Reissue
1. **Assignment Flow (`assign_delivery_task`)**:
   - Issue pickup OTP via `issue_otp(delivery, 'pickup')`.
   - Return plaintext pickup OTP once to the shelter in the assign response.
   - Do **NOT** generate or email doorstep OTP at assignment.
   - Send Adopter notification (email + in-app `Notification`): Inform them driver has been assigned with driver name, vehicle details, and contact number. Contains **NO OTP**.
   - Remove legacy `dispatch_doorstep_otp` calls from assignment.
2. **Pickup Verification Flow (`pickup`)**:
   - Driver submits pickup OTP at shelter.
   - On successful verification:
     - Delivery transitions to `out_for_delivery`, `dispatched_at = timezone.now()`.
     - Call `issue_otp(delivery, 'doorstep')` (expiry = now + `OTP_TTL_MINUTES`).
     - Email doorstep OTP plaintext to customer.
     - Set `doorstep_otp_sent_at = timezone.now()`.
     - In-app notification text: `"Driver is on the way with {pet_name}. Check your email for your confidential verification OTP."` (contains **no code**).
     - Idempotent: if `doorstep_otp_sent_at` is already set, do not re-email.
     - Email failure resilience: log exception, do not abort transition, leave `doorstep_otp_sent_at` NULL so shelter reissue can resend.
3. **Reissue Endpoint**:
   - Route: `POST /api/v1/shelter/deliveries/<id>/reissue-otp/`
   - Permissions: Authenticated shelter owning the delivery, or platform admin.
   - Payload: `{"purpose": "pickup" | "doorstep"}`
   - Action:
     - If purpose is `pickup`: issue new pickup OTP, return plaintext in response body `{"pickup_otp": "...", "expires_at": "..."}`.
     - If purpose is `doorstep`: issue new doorstep OTP, email plaintext to customer, set `doorstep_otp_sent_at`, return `{"detail": "Doorstep OTP reissued and emailed to adopter."}`.
4. **Serializer Hardening**:
   - `DeliveryDriverTaskSerializer`: Remove `pickup_otp`.
   - `ShelterDeliverySerializer`: Remove `pickup_otp` and `doorstep_otp`. Expose `pickup_otp_attempts`, `doorstep_otp_attempts`, `expires_at`, `doorstep_otp_expires_at`, `doorstep_otp_sent_at`.
   - `DeliveryCustomerSerializer`: Remove `doorstep_otp`.
5. **Strict `update_status` Transition Rules**:
   - Drivers may only set `'failed'` (from `'assigned'` or `'out_for_delivery'`), which frees the driver (`is_available = True`).
   - Reject `'out_for_delivery'`, `'delivered'`, `'in_transit'`, or case variations with HTTP 400.
   - `'out_for_delivery'` reachable ONLY via pickup OTP verification.
   - `'delivered'` reachable ONLY via doorstep OTP verification.

### Phase 5 — Verification Endpoints Hardening
For both `pickup` and `verify_doorstep` (canonical and legacy aliases):
1. **Permission**: Assigned driver only (HTTP 403 otherwise).
2. **Input Validation**: Exactly 6 numeric digits via regex `^\d{6}$`. Malformed input returns HTTP 400 without incrementing attempt counter.
3. **Concurrency Control**: `transaction.atomic()` with `select_for_update()` on `PetDelivery`.
4. **State Machine Checks**:
   - Pickup: requires `status == 'assigned'`.
   - Doorstep: requires `status == 'out_for_delivery'`. If already `'delivered'`, return HTTP 200 idempotently without re-executing transitions. Other states return HTTP 409 Conflict.
5. **Lockout & Expiry**:
   - `attempts >= OTP_MAX_ATTEMPTS`: HTTP 423 Locked (`{"detail": "Maximum OTP verification attempts exceeded. Please contact the shelter to reissue an OTP."}`).
   - Expired (`now > expires_at`): HTTP 410 Gone (`{"detail": "OTP has expired. Please contact the shelter to reissue an OTP."}`).
6. **Failed Attempt Handling**:
   - Increment attempt counter and commit immediately.
   - Compute `attempts_remaining = max(0, OTP_MAX_ATTEMPTS - attempts)`.
   - Return HTTP 400 Bad Request with remaining attempts.
7. **Successful Doorstep Verification**:
   - Atomic update:
     - `delivery.status = 'delivered'`
     - `delivery.delivered_at = timezone.now()`
     - `delivery.doorstep_otp_hash = None` (clear hash)
     - `delivery.driver.is_available = True`
     - `delivery.application.pet.status = 'adopted'`
     - `delivery.application.status = 'completed'`
     - Payment evaluation: set `application.payment_status = 'paid'` if verified `Payment(status='paid')` exists or COD `cash_collected >= total_amount`; otherwise leave unchanged.
     - Send completion notification to customer.
8. **Rate Limiting**: Apply DRF throttle scope `'otp_verify'` (`10/min`).

### Phase 6 — URL Routing & Backward Compatibility
- Canonical routes: `/api/v1/shelter/deliveries/...` and `/api/v1/delivery/tasks/...`
- Maintain full compatibility with legacy routes (`/api/deliveries/...`, `/api/v1/deliveries/...`).
- Document legacy paths in routing module.

### Phase 7 — Comprehensive Testing & Validation Proof
- Write complete unit and API test suite in `apps/logistics/tests.py`.
- Verify every success and error status code (200, 201, 400, 403, 409, 410, 423).
- Test settings fail-closed behavior.
- Capture all execution outputs.

---

## 4. API Contract & Error Code Matrix

| Endpoint | Method | Role | Status Codes | Description |
|---|---|---|---|---|
| `/api/v1/shelter/deliveries/assign/` | POST | Shelter | 201, 400, 403, 404 | Assigns driver; returns plaintext `pickup_otp` once to shelter; no doorstep OTP created. |
| `/api/v1/shelter/deliveries/<id>/reissue-otp/` | POST | Shelter | 200, 400, 403, 404 | Reissues pickup or doorstep OTP; resets attempts & expiry; emails customer if doorstep. |
| `/api/v1/delivery/tasks/<id>/pickup/` | POST | Driver | 200, 400, 403, 409, 410, 423, 429 | Verifies pickup OTP; issues doorstep OTP & emails customer; moves to `out_for_delivery`. |
| `/api/v1/delivery/tasks/<id>/verify-doorstep/` | POST | Driver | 200, 400, 403, 409, 410, 423, 429 | Verifies customer doorstep OTP; marks `delivered`, `adopted`, `completed`. |
| `/api/v1/delivery/tasks/<id>/status/` | PATCH/POST | Driver | 200, 400, 403, 404 | Allows ONLY `status='failed'`; rejects `out_for_delivery`, `delivered`, or arbitrary strings. |

### HTTP Status Code Specifications:
- **200 OK**: Successful verification or idempotent retry of already delivered task.
- **201 Created**: Successful task assignment with initial pickup OTP issued.
- **400 Bad Request**: Malformed 6-digit input, invalid OTP with attempts remaining, or invalid status transition.
- **403 Forbidden**: Caller is not the assigned driver or authorized shelter owner.
- **409 Conflict**: Action not valid in current delivery state (e.g., verifying doorstep while still in `assigned`).
- **410 Gone**: OTP has expired beyond TTL.
- **423 Locked**: Maximum attempts (3) exceeded; requires shelter reissue.
- **429 Too Many Requests**: DRF throttle exceeded (10/min).

---

## 5. Non-Goals & Scope Lock
- No frontend files or tooling touched.
- No modifications to payments gateway logic beyond settings fail-closed enforcement and `payment_status` conditional evaluation.
- No dropping or renaming of database tables or columns.
- No plaintext OTP logging or storage.

---

## 6. COD Cash Collection Design (PROPOSED)

### 6.1 Context & Problem Statement
Currently in the data model, `PetDelivery.cash_collected` exists as a Decimal field (`max_digits=10, decimal_places=2, default=0.00`), but **no API endpoint or service method writes to `cash_collected`**. Furthermore, for cash on delivery (COD) orders, there is currently no mechanism ensuring the physical cash is collected before the driver completes the delivery handoff via doorstep OTP.

### 6.2 Recommended Design (At Doorstep by Driver)
A dedicated driver-only cash collection endpoint called right before doorstep OTP verification.

#### Endpoint Specification
- **Path**: `POST /api/v1/delivery/tasks/<id>/collect-cash/` (and legacy alias `POST /api/deliveries/<id>/collect-cash/`)
- **Permission**: `IsAuthenticated`, `IsAssignedDeliveryDriver` (only the currently assigned delivery driver)
- **Request Payload**:
  ```json
  {
    "amount": "250.00"
  }
  ```
- **Execution Lifecycle (under `select_for_update` row lock)**:
  1. **Status & Driver Validation**:
     - Verify caller is `delivery.driver`.
     - Verify `delivery.status == 'out_for_delivery'` (returns 409 Conflict if in `assigned`, `delivered`, or `failed`).
     - Verify `application.payment_method == 'cod'` (returns 400 Bad Request if online/prepaid).
  2. **Amount Validation**:
     - Validate `amount` is a positive Decimal (`amount > 0`).
     - Validate cumulative amount `delivery.cash_collected + amount <= application.total_amount` (rejects overcollection with 400 Bad Request).
  3. **State Mutation**:
     - Increment `delivery.cash_collected += amount`.
     - Record a ledger entry in `Payment` model: `status='paid'`, `amount=amount`, `payment_method='cod'`, `transaction_id=f"COD-{delivery.public_id}-{timestamp}"`.
     - If `delivery.cash_collected >= application.total_amount`:
       - Set `application.payment_status = 'paid'`.
       - `application.save(update_fields=['payment_status'])`.
     - `delivery.save(update_fields=['cash_collected'])`.
  4. **Crucial Rule on Doorstep OTP Verification Guard**:
     - For `application.payment_method == 'cod'` where `application.total_amount > 0`:
       - In `verify_doorstep_service()`, doorstep OTP verification **MUST check that `delivery.cash_collected >= application.total_amount`**.
       - If `delivery.cash_collected < application.total_amount`: **return 409 Conflict** with detail: `"Cash collection incomplete. Total amount Rs. {total_amount} must be collected before completing doorstep handoff."`
       - Rationale: The driver cannot hand over the pet without full payment.
     - For `application.total_amount == 0` (free adoption or 100% waiver):
       - Cash collection is not required; treated as paid/not applicable.
  5. **Response Payload**:
     ```json
     {
       "message": "Cash collected successfully.",
       "cash_collected": "250.00",
       "total_amount": "250.00",
       "payment_status": "paid",
       "ready_for_doorstep_verification": true
     }
     ```

### 6.3 Alternative Design (Settled at Shelter by Staff)
Allow the delivery to complete at the doorstep without cash recording in the mobile app; the driver collects cash offline, and shelter staff records cash settlement when the driver returns to the shelter.

#### Comparison Matrix: Recommended vs. Alternative

| Dimension | Recommended Design (Driver at Doorstep) | Alternative Design (Shelter Settle) |
|---|---|---|
| **Fraud Prevention** | **High**: Pet cannot be released via doorstep OTP until cash collection is recorded in DB. | **Low**: Driver can verify doorstep OTP, hand over pet, and claim customer refused to pay or dispute cash amount. |
| **Real-time Accuracy** | **Immediate**: Adopter and platform see real-time payment status transition. | **Delayed**: Payment remains `pending` for hours/days until driver returns to shelter. |
| **Driver UX** | Requires one extra API action in driver app before verifying doorstep OTP. | Simpler mobile flow; driver only verifies OTP. |
| **Shelter Overhead** | None on delivery; shelter staff only reconciles aggregate cash payout from driver. | High: Shelter admin must manually locate each delivery and input cash collected. |
| **Edge Case Handling** | Adopter can pay partial cash + partial online UPI at doorstep before driver confirms. | Inability to track partial payments at the point of delivery. |

### 6.4 Test Cases for Recommended Design
The following test suite will be implemented upon approval:
1. `test_driver_collect_cash_success`: Assigned driver records exact cash amount; `cash_collected` updates, `payment_status` transitions to `paid`.
2. `test_collect_cash_forbidden_for_unassigned_driver_or_customer`: Non-driver caller receives 403 Forbidden.
3. `test_collect_cash_fails_if_not_out_for_delivery`: Calling in `assigned` or `delivered` state returns 409 Conflict.
4. `test_collect_cash_fails_for_online_payment_method`: Calling on an order with `payment_method='online'` returns 400 Bad Request.
5. `test_collect_cash_rejects_negative_or_zero_or_excessive_amount`: Rejects `0`, negative amounts, or amounts exceeding `total_amount` with 400 Bad Request.
6. `test_doorstep_otp_blocked_until_full_cash_collected`: Doorstep OTP verification returns 409 Conflict if `cash_collected < total_amount`.
7. `test_doorstep_otp_allowed_when_total_amount_is_zero`: Zero-cost adoptions bypass cash collection requirement.
8. `test_doorstep_otp_succeeds_after_full_cash_collection`: Full cash collection followed by correct doorstep OTP marks delivery `delivered` and application `completed`.

---

## 7. Milestone 2 — Payment Hardening, Verified Shelters & Authorization Matrix

### 7.1 Vision & Goals
Milestone 2 hardens the platform's financial and role-based integrity:
1. **Payment Hardening**: Eliminates client-controlled amounts, enforces strict application ownership, ensures atomic idempotent order creation and verification, introduces a unified atomic `mark_payment_captured` state machine, and secures asynchronous webhook processing.
2. **Verified Shelters**: Enforces mandatory shelter verification (`is_verified=True`) on all platform write operations and restricts public pet discovery to verified shelters.
3. **Authorization Matrix**: Comprehensive permission matrix auditing and testing across all canonical and legacy endpoints to ensure zero cross-tenant or role-escalation leaks.

### 7.2 Task 1: Payment Hardening Specification
1. **`create-order` Endpoint**:
   - **Serializer Validation**: Parse input using `PaymentOrderCreateSerializer`. Rejects non-UUID delivery identifiers without raw type casting.
   - **Ownership Check**: `request.user` must match `delivery.application.applicant`. Non-matching users receive `404 Not Found`.
   - **Server-Side Amount Derivation**: Any client-submitted amount is strictly ignored. The order amount in paise is computed directly from `application.total_amount` using `Decimal`: `int(round(Decimal(str(application.total_amount)) * 100))`.
   - **State Rejections**:
     - `application.total_amount <= 0`: 400 Bad Request (`"Total amount must be greater than zero."`).
     - `application.status != 'approved'`: 409 Conflict (`"Application must be approved."`).
     - `application.payment_method == 'cod'`: 409 Conflict (`"Cannot initiate online payment for Cash on Delivery application."`).
     - `application.payment_status == 'paid'`: 409 Conflict (`"Payment already completed for this application."`).
     - `delivery.status not in ('assigned', 'out_for_delivery')`: 409 Conflict (`"Delivery is not in a payable status."`).
   - **Row Locking & Idempotent Order Reuse**: Inside an atomic transaction with `application = AdoptionApplication.objects.select_for_update().get(...)`, if an unpaid `Payment` row already exists for this delivery with the same amount in paise, reuse and return its existing Razorpay order data rather than creating a duplicate order.
   - **Payment Method Transition**: Set `application.payment_method = 'online'` only if currently `'unset'` or `'online'`.

2. **`verify` Endpoint**:
   - **Atomic Lookup & Ownership**: Query `Payment` by `razorpay_order_id` with `select_for_update()`. If unknown -> 404 Not Found. If `payment.application.applicant != request.user` -> 404 Not Found.
   - **Idempotency**: If `payment.status in ('captured', 'paid')`, return 200 OK immediately.
   - **Amount Safety**: Confirm `payment.amount_in_paise == int(round(Decimal(str(payment.application.total_amount)) * 100))`. Mismatch returns 400 Bad Request.
   - **Cryptographic Signature Verification**: Verify HMAC-SHA256 signature using Razorpay SDK utility. If invalid, transition payment to `failed` and return 400 Bad Request.

3. **Unified `mark_payment_captured` State Machine**:
   - Centralized in `apps/logistics/payment_services.py` and called by both `verify` and `webhook`.
   - Defined status constants: `PaymentStatus.CREATED`, `PaymentStatus.PENDING`, `PaymentStatus.CAPTURED`, `PaymentStatus.FAILED` (plus legacy aliases `PaymentStatus.PAID`).
   - Transitions under row lock only from non-terminal states to `captured`.
   - Sets `payment.status = 'captured'`, `application.payment_status = 'paid'`, `delivery.payment_status = 'paid'`.

4. **`webhook` Endpoint**:
   - Configured with `permission_classes=[AllowAny]` and `authentication_classes=[]`.
   - Computes signature verification over raw `request.body`.
   - `payment.captured`: Compares payload amount and currency with the `Payment` row. On mismatch, logs a warning and leaves database unchanged.
   - `payment.failed`: Records failed status only if payment is not already captured (never overwrites captured payment).
   - Unknown order ID returns 200 OK without database mutation.
   - Generic error responses: Never leaks internal stack trace or `str(e)`.

### 7.3 Pickup Online Payment Guard (Proposal - Report Only)

#### Proposed Specification
When an adoption application has `payment_method == 'online'` and `total_amount > Decimal('0.00')`, the pet should not leave the shelter until payment has been captured.

In `DriverDeliveryViewSet.pickup()` / `verify_pickup_service()`:
1. Check `application.payment_method == 'online'` and `application.total_amount > Decimal('0.00')`.
2. If `application.payment_status != 'paid'`:
   - Return HTTP 409 Conflict with code `PAYMENT_REQUIRED` and message:
     `"Online payment incomplete. Total amount Rs. {total_amount} must be paid before dispatching driver for pickup."`
3. If `payment_method == 'cod'` or `total_amount == Decimal('0.00')`, pickup is allowed to proceed without prior online payment.

#### Required Test Suite for Proposal
1. `test_pickup_blocked_when_online_payment_not_captured`: Pickup OTP verification returns 409 Conflict if online payment is pending.
2. `test_pickup_allowed_when_online_payment_captured`: Pickup OTP verification succeeds when payment is captured.
3. `test_pickup_allowed_for_cod`: Cash on delivery allows pickup prior to doorstep payment.
4. `test_pickup_allowed_for_zero_cost_adoption`: Zero-amount adoption allows pickup without payment.

### 7.4 Task 2: Verified Shelters Specification
1. **Permission `IsVerifiedShelter`**:
   - Requires `request.user.role == 'shelter'` and `request.user.shelter_profile.is_verified is True`.
   - Superusers / Admins bypass verification check.
   - Unverified shelters attempting restricted actions receive HTTP 403 Forbidden with `{"code": "shelter_not_verified", "detail": "Shelter organization has not been verified by platform administrators."}`.
2. **Protected Write Actions**:
   - Pet Management: `ShelterPetViewSet` (create, update, partial_update, destroy) and `PetViewSet` writes.
   - Pet Media: `PetImageCreateView` (POST).
   - Medical Records: `PetMedicalRecordViewSet` (POST).
   - Applications: `ShelterApplicationViewSet` (review, approve, reject) and `AdoptionApplicationDetailView` (PATCH).
   - Fleet & Deliveries: `ShelterDeliveryViewSet.assign`, `ShelterDeliveryViewSet.reissue_otp`, `ShelterDriverCreateView` (POST).
3. **Public Catalog Visibility**:
   - `PetViewSet.list` and `PetViewSet.retrieve` filter queryset by `shelter__is_verified=True`.
   - Admins and the owning shelter can still view unverified shelter pets.

### 7.5 Task 3: Authorization Matrix Specification
- Exhaustive audit of all canonical (`/api/v1/`) and legacy routes with tests verifying:
  1. Anonymous requests -> 401 Unauthorized.
  2. Incorrect user role -> 403 Forbidden.
  3. Correct role but non-owner/cross-tenant -> 403 Forbidden or 404 Not Found.
  4. Legitimate owner/participant -> 200/201/204 Success.

---

*Last updated: 2026-10-15*

