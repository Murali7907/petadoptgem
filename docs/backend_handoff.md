# KindHeart Pet Adoption & Logistics — Backend Handoff Guide

This guide is the complete API handoff manual for frontend and mobile client developers integrating with the KindHeart Backend. It documents authentication lifecycles, role-based endpoint permissions, error codes, delivery state machines, and canonical workflows.

---

## 1. Authentication Lifecycle & Rate Throttling

KindHeart uses **JSON Web Tokens (SimpleJWT)** with embedded role claims and sliding expiration.

### 1.1 Token & Account Management Endpoints

| Endpoint | Method | Purpose | Request Body | Response Payload |
|---|---|---|---|---|
| `/api/v1/auth/register/` | `POST` | User registration | `username`, `email`, `password`, `role`, `phone`, `address`, *(optional shelter profile fields)* | `UserRegistration` representation |
| `/api/v1/auth/login/` | `POST` | User login | `username`, `password` | `{"access": "<jwt>", "refresh": "<jwt>"}` |
| `/api/v1/auth/token/refresh/` | `POST` | Rotate access token | `{"refresh": "<jwt>"}` | `{"access": "<new_jwt>", "refresh": "<optional_rotated_jwt>"}` |
| `/api/v1/auth/token/verify/` | `POST` | Validate token validity | `{"token": "<jwt>"}` | `200 OK` or `401 Unauthorized` |
| `/api/v1/auth/logout/` | `POST` | Blacklist refresh token | `{"refresh": "<jwt>"}` | `{"message": "Successfully logged out."}` |
| `/api/v1/auth/password-reset/` | `POST` | Request password reset link | `{"email": "<email>"}` | `{"message": "If an account with this email exists, a password reset link has been sent."}` (uniform 200, no enumeration) |
| `/api/v1/auth/password-reset/confirm/` | `POST` | Confirm password reset | `{"uid": "<uid>", "token": "<token>", "new_password": "<new_pass>"}` | `{"message": "Password has been reset successfully. Please log in with your new password."}` (blacklists all refresh tokens) |
| `/api/v1/auth/password/change/` | `POST` | Authenticated password change | `{"old_password": "<old>", "new_password": "<new>"}` | `{"message": "Password changed successfully. Please log in again with your new password."}` (blacklists all refresh tokens) |
| `/api/v1/auth/verify-email/` | `POST` | Confirm email verification | `{"token": "<token>"}` | `{"message": "Email address verified successfully.", "email_verified": true}` (idempotent) |
| `/api/v1/auth/resend-verification/` | `POST` | Resend verification email | Empty / Authenticated | `{"message": "If your email address is not yet verified, a new verification link has been sent."}` |

### 1.2 JWT Claims & Client Headers
Protected endpoints require the standard `Authorization` header:
```http
Authorization: Bearer <access_token>
```
The decoded access token contains:
- `user_id`: Primary database integer ID.
- `public_id`: Stable UUID string.
- `username`: Account handle.
- `role`: One of `customer`, `shelter`, `delivery`, `admin`.

### 1.3 Logout & Token Blacklisting
The `/api/v1/auth/logout/` endpoint accepts a refresh token and permanently registers it with `rest_framework_simplejwt.token_blacklist`.
- **Idempotency**: Submitting an already blacklisted token returns `200 OK` (`"message": "Successfully logged out (token already blacklisted)."`).
- **Malformed Tokens**: Passing invalid or expired tokens returns `400 Bad Request` with `{"error": "...", "code": "invalid_token"}`.

### 1.4 Rate Throttling Limits
All authentication endpoints are rate-limited per client IP:
- **`auth_login`**: `10 requests/min` (default). Configurable via `AUTH_LOGIN_THROTTLE_RATE`.
- **`auth_register`**: `5 requests/min` (default). Configurable via `AUTH_REGISTER_THROTTLE_RATE`.
- **`auth_refresh`**: `30 requests/min` (default). Configurable via `AUTH_REFRESH_THROTTLE_RATE`.
- **`auth_password_reset`**: `5 requests/min` (default). Configurable via `AUTH_PASSWORD_RESET_THROTTLE_RATE`.
- **`auth_password_reset_confirm`**: `5 requests/min` (default). Configurable via `AUTH_PASSWORD_RESET_CONFIRM_THROTTLE_RATE`.
- **`auth_resend_verification`**: `3 requests/min` (default). Configurable via `AUTH_RESEND_VERIFICATION_THROTTLE_RATE`.
- **`otp_verify`**: `5 requests/min` (default). Configurable via `OTP_THROTTLE_RATE`.
- **`shelter_cancel`**: `20 requests/min` (default). Configurable via `SHELTER_CANCEL_THROTTLE_RATE`.

When throttled, the server returns HTTP `429 Too Many Requests` with a `Retry-After` header in seconds.

### 1.5 Password Management & Email Verification Lifecycle
1. **Password Reset (No Account Enumeration)**:
   - `POST /api/v1/auth/password-reset/` always returns HTTP 200 with `{"message": "If an account with this email exists, a password reset link has been sent."}` regardless of whether the email is registered or active.
   - For active users, an email containing `{FRONTEND_BASE_URL}/reset-password?uid={uid}&token={token}` is queued after database commit. Tokens are single-use and expire after `PASSWORD_RESET_TIMEOUT` (default `3600` seconds / 1 hour).
   - `POST /api/v1/auth/password-reset/confirm/` validates password strength using `AUTH_PASSWORD_VALIDATORS` (returns 400 `weak_password`), ensures the token is valid and unconsumed (returns 400 `invalid_token`), resets the password, and blacklists all outstanding refresh tokens for the user.
2. **Authenticated Password Change**:
   - `POST /api/v1/auth/password/change/` checks the existing password (returns 400 `invalid_password`), validates the new password against `AUTH_PASSWORD_VALIDATORS` (returns 400 `weak_password`), updates the password, and immediately blacklists all active refresh tokens for the user.
3. **Email Verification**:
   - On registration, a verification email containing a cryptographically signed token (`TimestampSigner`) is dispatched to `{FRONTEND_BASE_URL}/verify-email?token={token}` after transaction commit. The token expires after `EMAIL_VERIFY_MAX_AGE` (default `86400` seconds / 24 hours).
   - `POST /api/v1/auth/verify-email/` marks `email_verified=True` and records `email_verified_at=now()`. It is idempotent and returns 400 `invalid_token` if tampered, expired, or claimed by a different email.
   - Profile email updates via `PUT/PATCH /api/v1/auth/me/` automatically reset `email_verified` to `False` and dispatch a new verification email after transaction commit.
   - **Existing Users & Admin Verification**: Existing users created prior to email verification start unverified (`email_verified=False`). Platform administrators can mark users verified directly in the Django Admin interface (`email_verified` and `email_verified_at` are exposed on `CustomUserAdmin`).
   - **Logistics Delivery Gate**: Attempting to assign a delivery task to an adopter whose email is not verified returns HTTP 409 `{"code": "adopter_email_unverified", "detail": "Cannot assign delivery task: adopter email address is unverified."}`.

### 1.6 JWT Token Lifetimes & Frontend Refresh Obligation
- **Access Token Lifetime**: 15 minutes by default (configurable via `ACCESS_TOKEN_MINUTES`). Short-lived access tokens limit exposure windows if a token is intercepted.
- **Refresh Token Lifetime**: 7 days by default (configurable via `REFRESH_TOKEN_DAYS`), with automatic rotation (`ROTATE_REFRESH_TOKENS = True`) and immediate blacklisting of rotated tokens (`BLACKLIST_AFTER_ROTATION = True`).
- **Frontend Token Refresh Obligation**: The frontend web application MUST securely store the refresh token and silently invoke `POST /api/v1/auth/token/refresh/` with `{"refresh": "<refresh_token>"}` to obtain a fresh access token before the 15-minute expiration or immediately upon catching an HTTP `401 Unauthorized` response with `code: "token_not_valid"`.

---

## 2. Role-Based Permissions Matrix

| Endpoint Route | HTTP Method | Allowed Roles | Required Permissions / Invariants |
|---|---|---|---|
| `/api/v1/pets/` | `GET` | Public | Open discovery with filters (`species`, `gender`, `breed`, `min_age`, `max_age`, `city`, `q`). Returns `shelter_name`, `shelter_city` (or `"location not provided"`), and `distance_km`. Sensitive coordinates and street addresses are never exposed. |
| `/api/v1/pets/<uuid:public_id>/` | `GET` | Public | Public pet detail with **Tiered Disclosure**: `pickup_address`, `latitude`, `longitude`, `phone`, and medical `document_url` are masked (`null`) for public/anonymous users and pending applicants; unmasked exclusively for approved/completed adopters, owning shelter staff, and admins. |
| `/api/v1/customer/favorites/` | `GET`, `POST` | `customer` | Authenticated customer only. |
| `/api/v1/customer/favorites/<uuid:pet_public_id>/` | `DELETE` | `customer` | Authenticated customer only. |
| `/api/v1/applications/` | `GET`, `POST` | `customer` | Submit and list personal adoption applications. |
| `/api/v1/applications/<uuid:public_id>/` | `GET` | `customer` | View application details with distance and fees. |
| `/api/v1/applications/<uuid:public_id>/cancel/` | `POST` | `customer` | Blocked (409) if online payment captured or delivery cash collected. |
| `/api/v1/payments/create-order/` | `POST` | `customer` | Initiates Razorpay checkout for approved online adoption task. |
| `/api/v1/payments/verify/` | `POST` | `customer` | Verifies cryptographic HMAC-SHA256 Razorpay payment signature. |
| `/api/v1/delivery/tasks/<uuid:public_id>/tracking/` | `GET` | `customer` | Live delivery status, driver info, and driver coordinates (legacy alias: `/api/deliveries/<uuid:public_id>/track/`). |
| `/api/v1/applications/<uuid:public_id>/review/` | `POST` | `customer` | Submit rating & review once delivery status is `delivered`. |
| `/api/v1/auth/me/` | `GET`, `PUT`, `PATCH` | Authenticated | Manage user profile and nested shelter profile (`shelter_profile`). |
| `/api/v1/shelter/pets/` | `GET`, `POST` | `shelter` | `POST` requires `is_verified=True`. |
| `/api/v1/shelter/pets/<uuid:public_id>/` | `GET`, `PUT`, `PATCH`, `DELETE` | `shelter` | Writes require `is_verified=True` and ownership. |
| `/api/v1/shelter/pets/<uuid:public_id>/images/` | `POST` | `shelter` | Upload pet photos (max 10MB, JPEG/PNG/WebP, validated by Pillow). Requires `is_verified=True`. |
| `/api/v1/shelter/pets/<uuid:public_id>/medical/` | `POST` | `shelter` | Upload medical records & PDF/image certificates (max 15MB, `%PDF-` signature check). Requires `is_verified=True`. |
| `/api/v1/shelter/applications/` | `GET` | `shelter` | Filter applications for shelter pets. |
| `/api/v1/shelter/applications/<uuid:public_id>/` | `GET` | `shelter` | View application, proof documents, and notes. |
| `/api/v1/shelter/applications/<uuid:public_id>/review/` | `POST` | `shelter` | Move application to `reviewing`. Requires `is_verified=True`. |
| `/api/v1/shelter/applications/<uuid:public_id>/approve/` | `POST` | `shelter` | Approve application; sets pet to `pending`, auto-rejects competitors. Requires `is_verified=True`. |
| `/api/v1/shelter/applications/<uuid:public_id>/reject/` | `POST` | `shelter` | Reject application. Blocked (409) if online payment captured or delivery cash collected. Requires `is_verified=True`. |
| `/api/v1/shelter/deliveries/` | `GET` | `shelter`, `admin` | List deliveries belonging to shelter with optional `cash_unsettled=true` filter. |
| `/api/v1/shelter/deliveries/drivers/` | `GET` | `shelter` | Lists active delivery drivers registered under the shelter. |
| `/api/v1/shelter/deliveries/assign/` | `POST` | `shelter` | Assigns driver; creates delivery task; generates pickup OTP. Requires `is_verified=True`. |
| `/api/v1/shelter/deliveries/<uuid:public_id>/cancel/` | `POST` | `shelter` | Cancels delivery. Blocked (409) if online payment captured or delivery cash collected. Requires `is_verified=True`. |
| `/api/v1/shelter/deliveries/<uuid:public_id>/cancel-and-refund/` | `POST` | `shelter`, `admin` | Cancels active/failed delivery and triggers full refund of captured online payment. Throttled (`refund`, 10/min). Requires owning verified shelter or admin. |
| `/api/v1/shelter/deliveries/<uuid:public_id>/reconcile-cash/` | `POST` | `shelter`, `admin` | Reconciles and settles collected COD cash on delivered tasks. Requires owning verified shelter or admin. |
| `/api/v1/shelter/deliveries/<uuid:public_id>/reissue-otp/` | `POST` | `shelter` | Reissues pickup or doorstep OTP after expiry or lockout. Requires `is_verified=True`. |
| `/api/v1/shelter/reviews/` | `GET` | `shelter` | View customer ratings and feedback. |
| `/api/v1/delivery/tasks/driver-active/` | `GET` | `delivery` | Active delivery task assigned to driver (legacy alias: `/api/deliveries/driver-active/`). |
| `/api/v1/delivery/tasks/<uuid:public_id>/tracking/` | `GET` | `delivery`, `customer` | Task details including addresses and customer contact (legacy alias: `/api/deliveries/<uuid:public_id>/track/`). |
| `/api/v1/delivery/tasks/<uuid:public_id>/pickup/` | `POST` | `delivery` | Verifies pickup OTP; validates payment guard; transitions to `out_for_delivery`. |
| `/api/v1/delivery/tasks/<uuid:public_id>/collect-cash/` | `POST` | `delivery` | Driver records cash collected from customer for COD. |
| `/api/v1/delivery/tasks/<uuid:public_id>/verify-doorstep/` | `POST` | `delivery` | Verifies doorstep OTP; validates cash guard; transitions to `delivered`. |
| `/api/v1/delivery/tasks/<uuid:public_id>/status/` | `PATCH` | `delivery` | Driver reports delivery failure or status updates. |
| `/api/v1/delivery/tasks/<uuid:public_id>/ping/` | `POST` | `delivery` | Updates driver's live GPS coordinates (alias: `/location/`). |
| `/api/v1/delivery/reviews/` | `GET` | `delivery` | View driver rating and customer feedback. |
| `/api/v1/auth/admin/dashboard/stats/` | `GET` | `admin` | Aggregated platform metrics and KPIs. |
| `/api/v1/auth/admin/shelters/` | `GET` | `admin` | List shelters with verification filter. |
| `/api/v1/auth/admin/shelters/<uuid:public_id>/verify/` | `POST` | `admin` | Set `is_verified = True/False`. |
| `/api/v1/auth/drivers/create/` | `POST` | `admin` | Provision new delivery drivers. |

---

## 3. Structured Error Code Catalogue

All high-impact errors return structured JSON payloads with a standard `"code"` string for client-side programmatic branching:

```json
{
  "code": "<error_code>",
  "detail": "<human_readable_message>"
}
```

For invalid OTP attempts, the payload includes remaining attempts:
```json
{
  "code": "otp_invalid",
  "detail": "Invalid OTP. 2 attempt(s) remaining.",
  "attempts_remaining": 2
}
```

| HTTP Status | Error Code (`"code"`) | Occurring Endpoints | Meaning & Client Remediation |
|---|---|---|---|
| `400 Bad Request` | `invalid_token` | `/api/v1/auth/logout/` | The refresh token provided in the logout request is malformed, corrupted, or has an invalid signature. Prompt user to clear local storage. |
| `400 Bad Request` | `adopter_email_missing` | `/api/v1/shelter/deliveries/assign/` | Delivery task cannot be assigned because the applicant account has no email address. The shelter cannot dispatch the delivery until the customer adds an email address to receive the doorstep OTP. |
| `400 Bad Request` | `validation_error` | Delivery action endpoints (`pickup/`, `verify-doorstep/`, `collect-cash/`, `reissue-otp/`, `cancel/`, legacy `verify-otp/`) | Request body is missing required parameters or payload data failed validation (e.g., OTP is not 6 digits, missing required field, non-positive cash amount). |
| `400 Bad Request` | `otp_invalid` | `/api/v1/delivery/tasks/<id>/pickup/`<br>`/api/v1/delivery/tasks/<id>/verify-doorstep/`<br>`/api/v1/delivery/tasks/<id>/verify-otp/` | Submitted OTP is incorrect. Includes `attempts_remaining` integer count. Decrements allowed attempts counter. |
| `403 Forbidden` | `shelter_not_verified` | All shelter write endpoints | The shelter profile has `is_verified = False`. The platform administrator must verify the shelter license/profile in the admin dashboard before pets or deliveries can be managed. |
| `409 Conflict` | `invalid_state` | Delivery action endpoints (`pickup/`, `verify-doorstep/`, `collect-cash/`, `reissue-otp/`, `cancel/`, legacy `verify-otp/`) | Delivery task is not in the required status for the requested transition (e.g., attempting pickup on an unassigned or already delivered task, or cancelling an already completed delivery). |
| `409 Conflict` | `payment_required` | `/api/v1/delivery/tasks/<id>/pickup/` | Pickup OTP verification was rejected because the application payment method is `'online'`, but no captured Razorpay payment exists. Driver must wait until the customer completes checkout. |
| `409 Conflict` | `payment_method_required` | `/api/v1/delivery/tasks/<id>/pickup/` | Pickup OTP verification was rejected because the application payment method is `'unset'` and `total_amount > 0`. Shelter or adopter must designate payment method before pet leaves the facility. |
| `409 Conflict` | `cash_not_collected` | `/api/v1/delivery/tasks/<id>/verify-doorstep/` | Doorstep OTP verification was rejected because the delivery is Cash on Delivery (COD) and the driver has not yet called `collect-cash` with `cash_collected >= total_amount`. Driver must collect cash first. |
| `409 Conflict` | `cash_already_collected` | `/api/v1/delivery/tasks/<id>/collect-cash/` | Driver attempted to record cash collection on a delivery where cash has already been logged. |
| `409 Conflict` | `cash_collected` | `/api/v1/shelter/deliveries/<id>/cancel/`<br>`/api/v1/shelter/deliveries/<id>/cancel-and-refund/` | Shelter attempted to cancel a delivery task after the driver physically collected cash from the customer. The shelter must reconcile cash with the driver directly. |
| `409 Conflict` | `delivery_completed` | `/api/v1/shelter/deliveries/<id>/cancel-and-refund/` | Cannot cancel or refund an already delivered order. Returns and post-delivery refunds are out of scope. |
| `409 Conflict` | `no_captured_payment` | `/api/v1/shelter/deliveries/<id>/cancel-and-refund/` | No captured online payment was found for the delivery or application. |
| `409 Conflict` | `refund_in_progress` | `/api/v1/shelter/deliveries/<id>/cancel-and-refund/` | A refund transaction is currently created or processing with the payment gateway. |
| `409 Conflict` | `refund_required` | - `/api/v1/applications/<id>/cancel/`<br>- `/api/v1/shelter/applications/<id>/reject/`<br>- `/api/v1/shelter/deliveries/<id>/cancel/`<br>- `/api/v1/adoptions/<id>/status/` | An online Razorpay payment has already been captured (`PaymentStatus.CAPTURED`), or delivery cash has been collected. The application or delivery cannot be cancelled/rejected until shelter staff arranges a refund with the customer. |
| `410 Gone` | `otp_expired` | `/api/v1/delivery/tasks/<id>/pickup/`<br>`/api/v1/delivery/tasks/<id>/verify-doorstep/`<br>`/api/v1/delivery/tasks/<id>/verify-otp/` | The OTP has expired (standard 24-hour TTL). Contact shelter staff to reissue an OTP via `reissue-otp/`. |
| `423 Locked` | `otp_locked` | `/api/v1/delivery/tasks/<id>/pickup/`<br>`/api/v1/delivery/tasks/<id>/verify-doorstep/`<br>`/api/v1/delivery/tasks/<id>/verify-otp/` | Maximum failed OTP attempts exceeded (3 attempts). The OTP is permanently locked. Contact shelter staff to reissue an OTP via `reissue-otp/`. |
| `502 Bad Gateway` | `refund_failed` | `/api/v1/shelter/deliveries/<id>/cancel-and-refund/` | The payment gateway rejected the refund request or encountered a network error. Client may safely retry without double-refund risk. |

---

## 4. Delivery State Machine & Verification Gates

The KindHeart logistics lifecycle is strictly guarded by two cryptographically hashed 6-digit one-time passwords (OTPs) and payment validation checks.

```
       [Shelter Assigns Driver]
                  │
                  ▼
              ASSIGNED
                  │
                  │  Gate 1: Pickup OTP Verification
                  │  (Payment pre-check: online payment must be captured;
                  │   or method is COD)
                  ▼
          OUT_FOR_DELIVERY
                  │
                  │  Gate 2: Cash Collection (If COD)
                  │  POST /delivery/tasks/<id>/collect-cash/
                  ▼
                  │  Gate 3: Doorstep OTP Verification
                  │  (Payment pre-check: COD cash must be collected in full)
                  ▼
              DELIVERED
      (Pet: 'adopted' | App: 'completed')
```

### 4.1 Gate 1: Driver Assignment
- **Action**: Shelter calls `POST /api/v1/shelter/deliveries/assign/` with `application_public_id`, `driver_public_id`, and `payment_method` (`'online'` or `'cod'`).
- **Invariants**:
  - Application must be in `approved` status.
  - Adopter must have an email address; otherwise returns `400 {"code": "adopter_email_missing"}`.
  - Driver `is_available` is flipped to `False`.
  - A cryptographically secure 6-digit numeric **Pickup OTP** is generated, SHA-256 hashed with salt, and stored on the delivery task (`pickup_otp_hash`).
  - Shelter receives the raw pickup OTP in the API response and shelter dashboard.
  - Customer receives an email and in-app notice that a driver is assigned (driver name, vehicle info; **NO OTP**).

### 4.2 Gate 2: Shelter Pickup & Dispatch
- **Action**: Driver arrives at shelter, inspects pet, and asks shelter staff for the 6-digit Pickup OTP. Driver submits `POST /api/v1/delivery/tasks/<id>/pickup/` with `{"otp": "123456"}`.
- **Payment Guard**:
  - If `payment_method == 'online'`: Verifies that a `Payment` record exists with `status in ('captured', 'paid')`. If not, returns `409 {"code": "payment_required"}`. Consumes **0** OTP attempts.
  - If `payment_method == 'unset'` and `total_amount > 0`: Returns `409 {"code": "payment_method_required"}`. Consumes **0** OTP attempts.
- **OTP Verification**:
  - Hash-only verification against `pickup_otp_hash`.
  - Maximum 3 attempts (`pickup_otp_attempts`). Failed attempt decrements remaining count (`400 Bad Request`). Third failure permanently locks task (`423 Locked`).
- **State Transition on Success**:
  - `delivery.status` -> `'out_for_delivery'`.
  - `delivery.dispatched_at` -> timestamp.
  - Pickup OTP hash cleared.
  - A 6-digit **Doorstep OTP** is generated, SHA-256 hashed, and stored (`doorstep_otp_hash`).
  - Doorstep OTP is emailed to the adopter via `transaction.on_commit` (`doorstep_otp_sent_at` timestamp recorded).

### 4.3 Gate 3: Doorstep Delivery & Cash Collection
- **Cash on Delivery (COD)**:
  - If the order is COD, driver collects cash at the customer doorstep and calls `POST /api/v1/delivery/tasks/<id>/collect-cash/` with `{"amount_collected": "550.00"}`.
  - Records `delivery.cash_collected`.
- **Doorstep OTP Verification**:
  - Driver asks customer for their emailed Doorstep OTP and calls `POST /api/v1/delivery/tasks/<id>/verify-doorstep/` with `{"otp": "654321"}`.
  - **Payment Guard**: If `payment_method == 'cod'` and `delivery.cash_collected < application.total_amount`, verification is refused with `409 {"code": "cash_not_collected"}`. Consumes **0** OTP attempts.
  - **OTP Verification**: Verifies against `doorstep_otp_hash` (max 3 attempts).
- **State Transition on Success**:
  - `delivery.status` -> `'delivered'`.
  - `delivery.delivered_at` -> timestamp.
  - `application.status` -> `'completed'`.
  - `pet.status` -> `'adopted'`.
  - `driver.is_available` -> `True` (driver freed for new assignments).
  - `application.payment_status` -> `'paid'`.

### 4.4 Cancel & Refund Lifecycle (Exit Path for Paid Applications)
- **Initiator**: Owning verified shelter staff or platform administrator (`POST /api/v1/shelter/deliveries/<id>/cancel-and-refund/`). Adopters cannot trigger refunds directly.
- **Allowed States**: Delivery `status in ('assigned', 'out_for_delivery', 'failed')` with a captured online payment. Blocked if delivery is `delivered` (`409 {"code": "delivery_completed"}`).
- **Execution Order**:
  1. Validate preconditions under `select_for_update` on `Payment`.
  2. Idempotent check: if already refunded, return 200 immediately.
  3. Release row locks, then call Razorpay refund API (`client.payment.refund`) for the full captured amount with idempotency key.
  4. If gateway fails, return `502 {"code": "refund_failed"}`; state remains unchanged and retry is allowed.
  5. In one atomic transaction upon gateway success:
     - `delivery.status` -> `'failed'` (if active).
     - `driver.is_available` -> `True` (driver freed).
     - `application.status` -> `'cancelled'`.
     - `pet.status` -> `'available'` (pet made available for new adoptions; competing applications stay rejected).
     - `payment.status`, `application.payment_status`, `delivery.payment_status` -> `'refunded'`.
     - Email and in-app notifications dispatched to adopter via `transaction.on_commit`.
- **Known Manual Path**: Deliveries where cash was already physically collected by driver (`cash_collected > 0`) stay blocked (`409 {"code": "cash_collected"}`). Digital refund cannot process physical cash; shelter must reconcile directly with driver.

### 4.5 Late Capture Auto-Refund & Webhooks
- If a customer's online payment is captured *after* their application was already cancelled/rejected or delivery failed/cancelled:
  - System captures payment record and automatically schedules `issue_refund(source='late_capture')` via `transaction.on_commit`.
  - The `retry_pending_refunds` management command safely retries any pending or failed refunds older than N minutes idempotently.
- Webhooks for `refund.processed` and `refund.failed` are handled idempotently via `POST /api/v1/payments/webhook/` (public auth, signature verified). Unknown refund IDs return 200 ignored.

### 4.6 COD Cash Reconciliation & Shelter Money Flow
- **Workflow**:
  1. Driver collects physical cash at doorstep and verifies doorstep OTP. Delivery status transitions to `'delivered'`.
  2. Cash remains unsettled (`is_cash_settled_with_shelter = False`).
  3. Shelter queries unsettled deliveries via `GET /api/v1/shelter/deliveries/?cash_unsettled=true`.
  4. Once physical cash is handed over, shelter calls `POST /api/v1/shelter/deliveries/<id>/reconcile-cash/`.
  5. Under `select_for_update`: sets `is_cash_settled_with_shelter = True`, `cash_settled_at = now()`, `cash_settled_by = request.user`.
  6. Repeat calls return `200 OK` idempotently with `{"code": "already_settled"}`.

---

## 5. Happy Path per User Role

### 5.1 Adopter (Customer) Happy Path
1. **Search & Browse Pets**:
   - `GET /api/v1/pets/?species=dog&city=Bangalore`
   - Select a pet and inspect metadata and medical overview via `GET /api/v1/pets/<uuid:public_id>/`.
2. **Submit Adoption Application**:
   - `POST /api/v1/applications/`
   - Body: `{"pet_public_id": "...", "living_situation": "Own house", "contact_phone": "9876543210", ...}`.
   - Status is initialized to `submitted`. Total adoption fee and delivery distance are computed automatically.
3. **Wait for Shelter Approval**:
   - Polling or notification check: `GET /api/v1/applications/<uuid:public_id>/`.
   - Application moves from `submitted` -> `reviewing` -> `approved`.
4. **Complete Payment (Online Option)**:
   - Once approved, call `POST /api/v1/payments/create-order/` with `{"delivery_public_id": "..."}`.
   - Response contains Razorpay `order_id`, `amount` (in paise), and `currency`.
   - Frontend invokes Razorpay Checkout SDK.
   - On successful payment, call `POST /api/v1/payments/verify/` with `{"razorpay_order_id": "...", "razorpay_payment_id": "...", "razorpay_signature": "..."}`.
5. **Track Delivery & Receive Pet**:
   - Adopter receives email notification when a driver is assigned.
   - Track driver GPS coordinates in real-time via `GET /api/v1/delivery/tasks/<uuid:public_id>/tracking/` (legacy alias: `GET /api/deliveries/<uuid:public_id>/track/`).
   - When the driver picks up the pet from the shelter, the adopter receives an email containing the 6-digit **Doorstep OTP**.
   - Upon arrival, give the Doorstep OTP (and cash if COD) to the driver.
6. **Submit Review**:
   - After delivery is completed, submit feedback via `POST /api/v1/applications/<uuid:public_id>/review/` with `{"pet_rating": 5, "driver_rating": 5, "review_text": "Wonderful experience!"}`.

---

### 5.2 Shelter Staff Happy Path
1. **Profile Setup & Verification**:
   - Register account with `role: "shelter"`. Admin approves shelter (`is_verified = True`).
   - Inspect profile via `GET /api/v1/auth/me/` and update address / coordinates via `PATCH /api/v1/auth/me/`.
2. **Pet Listing Management**:
   - Create pet: `POST /api/v1/shelter/pets/` with `{"name": "Buddy", "species": "dog", ...}`.
   - Upload pet photos: `POST /api/v1/shelter/pets/<public_id>/images/` (multipart file upload).
   - Upload vaccination / medical certs: `POST /api/v1/shelter/pets/<public_id>/medical/` (multipart PDF/image upload).
3. **Application Review & Decision**:
   - Check incoming applications: `GET /api/v1/shelter/applications/?status=submitted`.
   - Mark under review: `POST /api/v1/shelter/applications/<id>/review/`.
   - Approve application: `POST /api/v1/shelter/applications/<id>/approve/`.
     - *System Side-Effects*: Pet status moves to `'pending'`; all competing applications for this pet are automatically rejected.
4. **Driver Assignment & Pet Handover**:
   - Find active drivers: `GET /api/v1/shelter/deliveries/drivers/`.
   - Assign driver: `POST /api/v1/shelter/deliveries/assign/` with `{"application_public_id": "...", "driver_public_id": "...", "payment_method": "online"}`.
   - Note the 6-digit **Pickup OTP** from the response.
   - When the driver arrives at the shelter, provide the Pickup OTP after handing over the pet.

---

### 5.3 Delivery Driver Happy Path
1. **Go On-Duty**:
   - Set status to available: `PATCH /api/v1/auth/me/` with `{"is_available": true}`.
2. **Receive Delivery Task**:
   - Fetch assigned active task: `GET /api/v1/delivery/tasks/driver-active/` (or `GET /api/v1/delivery/tasks/active/`).
   - Inspect pickup shelter address and customer dropoff address: `GET /api/v1/delivery/tasks/<uuid:public_id>/tracking/` (legacy alias: `GET /api/deliveries/<uuid:public_id>/track/`).
3. **Pickup from Shelter**:
   - Arrive at shelter facility. Obtain 6-digit Pickup OTP from shelter staff.
   - Call `POST /api/v1/delivery/tasks/<id>/pickup/` with `{"otp": "123456"}`.
   - Task transitions to `out_for_delivery`.
4. **Transit & GPS Updates**:
   - Periodically stream vehicle coordinates: `POST /api/v1/delivery/tasks/<id>/ping/` (alias: `/location/`) with `{"latitude": 12.9716, "longitude": 77.5946}`.
5. **Doorstep Delivery & Handover**:
   - Arrive at customer address.
   - *If COD*: Collect cash payment and call `POST /api/v1/delivery/tasks/<id>/collect-cash/` with `{"amount_collected": "..."}`.
   - Request the 6-digit Doorstep OTP from the customer.
   - Call `POST /api/v1/delivery/tasks/<id>/verify-doorstep/` with `{"otp": "654321"}`.
   - Task transitions to `delivered`. Driver is automatically returned to available status (`is_available = True`).

---

## 6. Location Privacy & Tiered Disclosure Architecture

To protect rescue volunteers, residential shelters, and confidential clinical documents, KindHeart implements strict **Tiered Disclosure**:

### 6.1 Public Discovery Endpoints (`GET /api/v1/pets/` and `GET /api/v1/pets/<uuid:public_id>/`)
- **Public Pet List (`GET /api/v1/pets/`)**:
  - Exposes `shelter_name`, `shelter_city`, and `distance_km`.
  - When a shelter record lacks a `city`, the API returns `"location not provided"`. Raw physical pickup street addresses are never used as a fallback.
  - Geographical sorting (`distance_meters`) and distance calculations (`distance_km`) operate entirely on the backend via spherical Haversine SQL database expressions without streaming coordinates to the client.
- **Public Pet Detail (`GET /api/v1/pets/<uuid:public_id>/`)**:
  - **Masked Data (Public Visitors & Pending Applicants)**:
    - Anonymous users, general authenticated customers, and applicants with pending applications (`submitted` or `reviewing`) receive:
      - `shelter.pickup_address = null`
      - `shelter.latitude = null`
      - `shelter.longitude = null`
      - `shelter.phone = null`
      - `medical_overview.medical_records[].document_url = null`
  - **Unmasked Data (Authorized Parties)**:
    - Full street address, high-precision coordinates, personal phone number, and direct clinical document URLs are exposed exclusively to:
      1. Authenticated staff/user of the owning shelter.
      2. Platform administrators (`role='admin'`).
      3. Adopter with an adoption application for that pet in `'approved'` or `'completed'` status.

---

## 7. Shelter Profile Schema & Retrieval

Shelter profile and operational facilities are managed through the authenticated user endpoints `GET /api/v1/auth/me/` and `PATCH /api/v1/auth/me/` (or `GET /api/v1/me/` and `PATCH /api/v1/me/`).
The shelter profile is serialized as the nested `shelter_profile` object (`ShelterProfileSerializer`) within `UserProfileSerializer`.

### Fields Returned in `shelter_profile`:
- `public_id`: Unique UUID identifier of the shelter profile.
- `organization_name`: Registered organizational name of the shelter or rescue group.
- `license_no`: Government/operational animal welfare license number.
- `pickup_address`: Physical street address for driver pet pickup handoffs.
- `city`: Operational city / municipality.
- `latitude`: Geographic latitude for proximity search and route calculation.
- `longitude`: Geographic longitude for proximity search and route calculation.
- `is_active`: Operational activity status flag (`true`/`false`).
- `is_verified`: Administrative vetting and verification badge (`true`/`false`).
- `created_at`: Account registration timestamp.

### 7.1 Password Reset Timing Leak Defense & Asynchronous Dispatch
- **Endpoint**: `POST /api/v1/auth/password-reset/`
- **Mitigation**: To prevent user enumeration attacks via response timing discrepancies (where SMTP I/O for existing users would take 200–2000 ms vs ~5 ms for unknown emails), email dispatch is decoupled from the HTTP request cycle using a short-lived daemon thread started inside `transaction.on_commit` (`AUTH_PASSWORD_RESET_ASYNC_EMAIL = True`).
- **Response Uniformity**: Both existing and unknown email addresses return the identical HTTP 200 OK message in ~5–15 ms.
- **Determinism under Test**: During automated test execution (`'test' in sys.argv`), dispatch runs synchronously so unit test suites assert sent emails deterministically without flakiness.

---

## 8. Platform Runtime Environment

- **Backend Framework**: Django 6.1.2 (pinned in `requirements.lock.txt`), Django REST Framework 3.18.3
- **Database Engine**: PostgreSQL (tested with PostgreSQL 16 in CI, supports standard PostgreSQL 14+)
- **Schema & Documentation**: drf-spectacular 0.30.0 (OpenAPI 3.0)

---

## 9. Operational Maintenance & Automated Refund Schedulers

### 9.1 Background Execution Notice
> [!IMPORTANT]
> **Nothing runs `retry_pending_refunds` automatically within the Django web application process.**
>
> In production and staging deployments, an external operating system scheduler (such as Linux cron, systemd timer, or Windows Task Scheduler) **MUST** be configured to execute `python manage.py retry_pending_refunds` at regular intervals (recommended: 5 minutes) to ensure failed or unconfirmed refunds are safely reconciled and retried.

The command scans for `Refund` rows in `created` or `failed` status older than `REFUND_RETRY_AFTER_MINUTES` (configured via environment variable, defaulting to 10 minutes), checks the Razorpay API gateway for any existing processed refunds via `fetch_multiple_refund` to avoid duplicate payouts, and idempotently finishes the refund flow.

### 9.2 Scheduler Configuration Examples (5-Minute Interval)

#### Linux cron
Add the following line to the crontab of the deployment user (`crontab -e`):
```cron
*/5 * * * * cd /opt/petadopt/backend && /opt/petadopt/backend/.venv/bin/python manage.py retry_pending_refunds >> /var/log/petadopt/refund_retry.log 2>&1
```

#### Linux systemd Timer
Create the service unit file `/etc/systemd/system/petadopt-refund-retry.service`:
```ini
[Unit]
Description=PetAdopt Pending Refund Reconciler and Retry
After=network.target

[Service]
Type=oneshot
User=www-data
WorkingDirectory=/opt/petadopt/backend
ExecStart=/opt/petadopt/backend/.venv/bin/python manage.py retry_pending_refunds
StandardOutput=journal
StandardError=journal
```

Create the timer unit file `/etc/systemd/system/petadopt-refund-retry.timer`:
```ini
[Unit]
Description=Run PetAdopt Refund Retry every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
```
Enable and start the timer:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now petadopt-refund-retry.timer
```

#### Windows Task Scheduler (PowerShell / cmd)
Register a scheduled task running every 5 minutes:
```powershell
schtasks /create /tn "PetAdopt_Refund_Retry" /tr "C:\petadopt\backend\.venv\Scripts\python.exe C:\petadopt\backend\manage.py retry_pending_refunds" /sc minute /mo 5 /ru "SYSTEM"
```

---

## 7. Production Email Infrastructure & Delivery Safety

KindHeart uses standard Django SMTP (`django.core.mail.backends.smtp.EmailBackend`) without external heavy worker dependencies. Celery is intentionally deferred to protect user OTPs from plaintext exposure in message broker queues.

### 7.1 Environment Variables
| Variable | Development Default | Production Invariant (DEBUG=False) |
|---|---|---|
| `EMAIL_BACKEND` | `django.core.mail.backends.console.EmailBackend` | Must NOT be `console`, `locmem`, `dummy`, or `filebased`. Fails closed (`ImproperlyConfigured`). |
| `EMAIL_HOST` | `localhost` | Required; cannot be empty or contain `mock`. |
| `EMAIL_PORT` | `587` | Standard SMTP TLS port (or `465` for SSL). |
| `EMAIL_HOST_USER` | `""` | SMTP provider username or API identifier. |
| `EMAIL_HOST_PASSWORD` | `""` | SMTP provider password or API secret. |
| `EMAIL_USE_TLS` | `True` | Recommended for port 587. |
| `EMAIL_USE_SSL` | `False` | Set `True` if using port 465. |
| `EMAIL_TIMEOUT` | `10` | Socket timeout in seconds (prevents request threads from hanging). |
| `DEFAULT_FROM_EMAIL` | `KindHeart Pet Adoption <no-reply@kindheart.org>` | Required; cannot be empty or contain `mock`. |

### 7.2 Transactional & Fail-Safe Architecture
To prevent slow SMTP servers from blocking requests or rolling back database changes:
1. **Post-Commit Execution**: Every email dispatch (`send_driver_assigned_notice`, `dispatch_doorstep_otp`, `send_delivery_completed_notice`, cancellation notice, and refund notice) executes inside `transaction.on_commit(...)`.
2. **Fail-Safe Exception Handling**: Every `send_mail` call is wrapped in a `try...except Exception` block with structured error logging. An SMTP timeout or failure will never rollback an approved adoption, payment capture, or delivery state, and will never return a `500 Internal Server Error` to the user.

---

## 10. Production Deployment Guide

This section documents standard deployment procedures, production environment requirements, static asset serving, and background schedulers.

### 10.1 Environment Variables Checklist (.env.example)

| Environment Variable | Production Status | Description & Constraints |
|---|---|---|
| `SECRET_KEY` | **Required** | Cryptographic secret key (50+ random characters). Cannot start with `django-insecure`. |
| `DEBUG` | **Required** | Must be explicitly set to `False` in production. |
| `ALLOWED_HOSTS` | **Required** | Comma-separated list of fully qualified domain names and production IPs. |
| `DB_NAME` | **Required** | PostgreSQL database name (e.g. `pet_adoption`). |
| `DB_USER` | **Required** | PostgreSQL role username. |
| `DB_PASSWORD` | **Required** | PostgreSQL secure password. |
| `DB_HOST` | **Required** | PostgreSQL hostname/IP (e.g. RDS endpoint or private IP). |
| `DB_PORT` | Optional | Database port (defaults to `5432`). |
| `RAZORPAY_KEY_ID` | **Required** | Live Razorpay Key ID (`rzp_live_...`). Cannot contain `mock`. |
| `RAZORPAY_KEY_SECRET` | **Required** | Live Razorpay Key Secret. Cannot contain `mock`. |
| `RAZORPAY_WEBHOOK_SECRET` | **Required** | Live Razorpay Webhook Secret. Cannot contain `mock`. |
| `REFUND_RETRY_AFTER_MINUTES` | Optional | Minimum age in minutes of unconfirmed refunds before retry (defaults to `10`). |
| `EMAIL_BACKEND` | Optional | Defaults to `django.core.mail.backends.smtp.EmailBackend`. Cannot be console/locmem/dummy/filebased. |
| `EMAIL_HOST` | **Required** | Production SMTP host (e.g. `smtp.sendgrid.net`). Cannot contain `mock`. |
| `EMAIL_PORT` | Optional | SMTP port (defaults to `587`). |
| `EMAIL_HOST_USER` | Optional | SMTP username / API key. Required if SMTP authentication is enabled. |
| `EMAIL_HOST_PASSWORD` | Optional | SMTP password / API token. Required if SMTP authentication is enabled. |
| `EMAIL_USE_TLS` | Optional | Enable TLS encryption (defaults to `True`). |
| `EMAIL_USE_SSL` | Optional | Enable SSL encryption (defaults to `False`). |
| `EMAIL_TIMEOUT` | Optional | Network socket timeout in seconds (defaults to `10`). Protects request threads against SMTP hangs. |
| `DEFAULT_FROM_EMAIL` | **Required** | Sender email address (e.g. `KindHeart Pet Adoption <no-reply@kindheart.org>`). Cannot contain `mock`. |
| `USE_CLOUDINARY` | Optional | Set `True` to use Cloudinary CDN for images and raw documents (defaults to `False`). |
| `CLOUDINARY_CLOUD_NAME` | Required if Cloudinary | Cloudinary cloud name. Cannot contain `mock`. |
| `CLOUDINARY_API_KEY` | Required if Cloudinary | Cloudinary API key. Cannot contain `mock`. |
| `CLOUDINARY_API_SECRET` | Required if Cloudinary | Cloudinary API secret. Cannot contain `mock`. |
| `CORS_ALLOWED_ORIGINS` | Optional | Comma-separated list of allowed frontend web origins (defaults to localhost:3000). |
| `CSRF_TRUSTED_ORIGINS` | Optional | Comma-separated list of trusted CSRF origins for browser forms (defaults to localhost:3000). |
| `REDIS_URL` | **Required for Cluster** | Redis connection URI (e.g. `redis://127.0.0.1:6379/1`). **Mandatory** across multi-worker Gunicorn deployments for shared rate-limiting counters. |
| `OTP_TTL_MINUTES` | Optional | Delivery verification OTP validity period (defaults to `1440` minutes / 24 hours). |
| `OTP_MAX_ATTEMPTS` | Optional | Maximum incorrect verification attempts before OTP lockout (defaults to `3`). |
| `OTP_THROTTLE_RATE` | Optional | Rate limit for OTP verification endpoint (defaults to `10/min`). |
| `LOG_LEVEL` | Optional | Application logging level (defaults to `INFO`). |
| `SHELTER_CANCEL_THROTTLE_RATE` | Optional | Rate limit for shelter delivery cancellation (defaults to `20/min`). |
| `AUTH_LOGIN_THROTTLE_RATE` | Optional | Rate limit for user login endpoint (defaults to `10/min`). |
| `AUTH_REGISTER_THROTTLE_RATE` | Optional | Rate limit for registration endpoint (defaults to `5/min`). |
| `AUTH_REFRESH_THROTTLE_RATE` | Optional | Rate limit for token refresh endpoint (defaults to `30/min`). |
| `REFUND_THROTTLE_RATE` | Optional | Rate limit for cancel-and-refund endpoint (defaults to `10/min`). |
| `MAX_PET_IMAGE_SIZE_BYTES` | Optional | Maximum image upload size in bytes (defaults to `10485760` / 10MB). |
| `MAX_MEDICAL_DOC_SIZE_BYTES` | Optional | Maximum medical document upload size (defaults to `15728640` / 15MB). |
| `FRONTEND_BASE_URL` | **Required** when DEBUG=False | Frontend application URL (defaults to `http://localhost:3000` when `DEBUG=True`). Cannot contain `mock`. |
| `ACCESS_TOKEN_MINUTES` | Optional | JWT access token lifetime in minutes (defaults to `15`). Frontend must refresh access tokens using refresh token. |
| `REFRESH_TOKEN_DAYS` | Optional | JWT refresh token lifetime in days (defaults to `7`). |
| `PASSWORD_RESET_TIMEOUT` | Optional | Validity period of password reset tokens in seconds (defaults to `3600` / 1 hour). |
| `AUTH_PASSWORD_RESET_ASYNC_EMAIL` | Optional | Dispatch password reset emails asynchronously via short-lived daemon thread to eliminate timing leaks and account enumeration (defaults to `True` in production, `False` during test suite). |
| `EMAIL_VERIFY_MAX_AGE` | Optional | Validity period of signed email verification tokens in seconds (defaults to `86400` / 24 hours). |
| `AUTH_PASSWORD_RESET_THROTTLE_RATE` | Optional | Rate limit for password reset request endpoint (defaults to `5/min`). |
| `AUTH_PASSWORD_RESET_CONFIRM_THROTTLE_RATE` | Optional | Rate limit for password reset confirm endpoint (defaults to `5/min`). |
| `AUTH_RESEND_VERIFICATION_THROTTLE_RATE` | Optional | Rate limit for resend verification email endpoint (defaults to `3/min`). |
| `DATA_UPLOAD_MAX_MEMORY_SIZE` | Optional | Memory limit for request body uploads (defaults to `10485760`). |
| `FILE_UPLOAD_MAX_MEMORY_SIZE` | Optional | Memory limit before streaming files to disk (defaults to `15728640`). |
| `SECURE_SSL_REDIRECT` | Optional | Redirect all HTTP traffic to HTTPS (defaults to `True` when `DEBUG=False`). |
| `SECURE_HSTS_SECONDS` | Optional | HTTP Strict Transport Security duration (defaults to `31536000` / 1 year). |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | Optional | Include subdomains in HSTS policy (defaults to `True`). |
| `SECURE_HSTS_PRELOAD` | Optional | Request browser HSTS preload list enrollment (defaults to `True`). |
| `SESSION_COOKIE_SECURE` | Optional | Transmit session cookies only over HTTPS (defaults to `True`). |
| `CSRF_COOKIE_SECURE` | Optional | Transmit CSRF cookies only over HTTPS (defaults to `True`). |
| `SECURE_CONTENT_TYPE_NOSNIFF` | Optional | Set `X-Content-Type-Options: nosniff` header (defaults to `True`). |
| `SECURE_REFERRER_POLICY` | Optional | Referrer policy header (defaults to `same-origin`). |
| `SECURE_PROXY_SSL_HEADER_ENABLED` | Optional | Enable `X-Forwarded-Proto` header trust behind Nginx/ALB (defaults to `False`). |
| `SECURE_PROXY_SSL_HEADER_NAME` | Optional | Header name for proxy SSL termination (defaults to `HTTP_X_FORWARDED_PROTO`). |
| `SECURE_PROXY_SSL_HEADER_VALUE` | Optional | Expected header value indicating HTTPS (defaults to `https`). |

---

### 10.2 Production Application Server & Run Commands

#### Step 1: Apply Pending Database Migrations
Always apply database schema migrations before launching the application:
```bash
python manage.py migrate --noinput
```

#### Step 2: Compile & Compress Static Assets
Collect static assets (admin CSS/JS, Swagger UI, rest_framework browsable UI assets):
```bash
python manage.py collectstatic --noinput
```

#### Step 3: Run Application with Gunicorn
Run Gunicorn pointing to Django's WSGI application:
```bash
gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 4 \
    --threads 2 \
    --timeout 60 \
    --access-logfile - \
    --error-logfile -
```

---

### 10.3 Static File Serving with WhiteNoise
When `DEBUG=False` in production, static files (including Django Admin assets, OpenAPI docs, and static styling) are served directly by Python through **WhiteNoise**:
- **Middleware**: `whitenoise.middleware.WhiteNoiseMiddleware` is registered in `MIDDLEWARE` immediately following `SecurityMiddleware`.
- **Storage Backend**: `STORAGES['staticfiles']` uses `whitenoise.storage.CompressedManifestStaticFilesStorage`, which compresses assets and generates unique content hashes for immutable cache headers (`Cache-Control: max-age=31536000, public, immutable`).
- **Zero Reverse Proxy Dependency**: Django Admin and Swagger documentation render with full styles and icons out-of-the-box without needing Nginx static alias configuration.

---

### 10.4 Automated Refund Reconciliation Scheduling
The platform reconciles unconfirmed or failed payment refunds via `manage.py retry_pending_refunds`. Celery workers are deferred by design to eliminate plaintext OTP exposure in queues. Instead, operating system schedulers run the reconciliation job every 5 to 10 minutes:

#### Linux cron Example (Every 5 Minutes)
```cron
*/5 * * * * cd /opt/petadopt/backend && /opt/petadopt/backend/.venv/bin/python manage.py retry_pending_refunds >> /var/log/petadopt/refund_retry.log 2>&1
```

#### Windows Task Scheduler Example (Every 5 Minutes)
```powershell
schtasks /create /tn "PetAdopt_Refund_Retry" /tr "C:\petadopt\backend\.venv\Scripts\python.exe C:\petadopt\backend\manage.py retry_pending_refunds" /sc minute /mo 5 /ru "SYSTEM"
```

---

### 10.5 Distributed Throttling & Redis Requirement
When deploying Gunicorn with multiple worker processes (`--workers > 1`), rate-limiting counters **must** be stored centrally:
- **`REDIS_URL`**: When specified (e.g. `redis://127.0.0.1:6379/1`), Django configures `django.core.cache.backends.redis.RedisCache`.
- **Throttling Accuracy**: Rate limits across `auth_login` (10/min), `auth_register` (5/min), `shelter_cancel` (20/min), and `refund` (10/min) are enforced globally across all Gunicorn workers rather than per-worker memory.

---

*KindHeart Engineering Team — Production Backend API Specifications*


