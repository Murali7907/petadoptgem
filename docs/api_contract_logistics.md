# Logistics & OTP Verification API Contract

## Overview

This document specifies the canonical and legacy API contracts for the pet delivery and doorstep verification lifecycle in the Pet Adoption Platform backend (Django REST Framework).

---

## Security Model & Cryptographic Guarantees

1. **Zero Plaintext Persistence**: Plaintext OTPs (6 numeric digits) are never persisted in the database. Instead, HMAC-SHA256 digests (`hash = HMAC_SHA256(key=settings.SECRET_KEY, msg=f"{delivery.id}:{purpose}:{otp}")`) are stored in `pickup_otp_hash` and `doorstep_otp_hash`.
2. **Zero Serializer Leakage**: Serializers (`DeliveryDriverTaskSerializer`, `ShelterDeliverySerializer`, `DeliveryCustomerSerializer`) never include plaintext `pickup_otp` or `doorstep_otp`. Tracking endpoints expose timestamps (`doorstep_otp_sent_at`, `doorstep_otp_expires_at`) but never verification codes.
3. **Decoupled Verification Lifecycle**:
   - `assign`: Generates **only** the pickup OTP (returned once in response to the shelter). Doorstep OTP is **not** created at assignment.
   - `pickup`: Validates pickup OTP, transitions delivery to `out_for_delivery`, and issues the doorstep OTP, dispatching it to the adopter via email.
   - `verify_doorstep`: Validates doorstep OTP, transitions delivery to `delivered`, marks pet `adopted`, marks adoption `completed`, and frees the driver.
4. **Concurrency & Rate Protection**:
   - All verification endpoints employ row-level locking via `select_for_update()`.
   - Dedicated DRF throttle scope: `'otp_verify': '10/min'`. Note: A shared cache (e.g., Redis) is required in production for the limit to hold across workers.
   - Max attempt limit: `OTP_MAX_ATTEMPTS = 3` before permanent task lockout (`423 Locked`).
   - OTP Expiry: Configurable `OTP_TTL_MINUTES = 1440` (24h default). Expired OTPs return `410 Gone`.

---

## Canonical Endpoints

### 1. Assign Delivery Task
- **URL**: `POST /api/v1/shelter/deliveries/assign/` (Legacy alias: `POST /api/deliveries/assign/`)
- **Authentication**: Required (`Bearer <access_token>`)
- **Permissions**: `IsVerifiedShelter` required (role `shelter` with `shelter_profile.is_verified=True`, or platform administrator)
- **Request Body**:
  ```json
  {
    "application_ref": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "driver_ref": "4fa85f64-5717-4562-b3fc-2c963f66afa7",
    "payment_method": "cod"
  }
  ```
- **Responses**:
  - `201 Created`: Driver assigned successfully.
    ```json
    {
      "message": "Delivery task assigned successfully.",
      "public_id": "5fa85f64-5717-4562-b3fc-2c963f66afa8",
      "status": "assigned",
      "pickup_otp": "654321",
      "pickup_otp_expires_at": "2026-10-15T12:00:00Z"
    }
    ```
    *(Note: `pickup_otp` is returned in plaintext ONCE upon creation so shelter staff can verify driver handoff).*
  - `400 Bad Request`: Validation failure or driver unavailable.
  - `409 Conflict`:
    - `{"code": "adopter_email_missing"}`: Adopter does not have an email address registered (`409 {"code": "adopter_email_missing"}`).
    - Cannot reassign delivery when already in-flight (`out_for_delivery`) or completed (`delivered`). (Reassignment in `assigned` or `failed` safely frees previous driver, resets `dispatched_at`, and issues fresh OTPs).

---

### 2. Get Active Driver Task
- **URL**: `GET /api/v1/deliveries/driver-active/` (Legacy alias: `GET /api/deliveries/driver-active/`)
- **Authentication**: Required (`Bearer <access_token>`)
- **Permissions**: Delivery Driver Profile required
- **Responses**:
  - `200 OK`: Active task returned.
    ```json
    {
      "public_id": "5fa85f64-5717-4562-b3fc-2c963f66afa8",
      "status": "assigned",
      "customer_name": "Adopter Name",
      "customer_phone": "+919876543210",
      "destination_address": "123 Main St, City",
      "destination_pincode": "560001",
      "destination_latitude": "12.971600",
      "destination_longitude": "77.594600",
      "pet_summary": "Rocky (Dog, Golden Retriever)",
      "total_amount": "250.00",
      "payment_method": "cod",
      "cash_collected": "0.00",
      "pickup_otp_attempts": 0,
      "doorstep_otp_attempts": 0,
      "doorstep_otp_sent_at": null,
      "doorstep_otp_expires_at": null
    }
    ```
    *(Note: Plaintext OTP fields are strictly omitted).*
  - `404 Not Found`: No active assigned or in-transit delivery task.

---

### 3. Verify Pickup OTP at Shelter
- **URL**: `POST /api/v1/deliveries/{id}/pickup/` (Legacy alias: `POST /api/deliveries/{id}/pickup/`)
- **Authentication**: Required (`Bearer <access_token>`)
- **Permissions**: Delivery Driver assigned to this specific delivery
- **Rate Limit**: 10 requests / minute
- **Request Body**:
  ```json
  {
    "otp": "654321"
  }
  ```
- **Responses**:
  - `200 OK`: Pickup verified. Delivery transitioned to `out_for_delivery`. Doorstep OTP generated and emailed to customer.
    ```json
    {
      "message": "Pet picked up successfully. Status transitioned to out_for_delivery.",
      "status": "out_for_delivery",
      "doorstep_otp_sent_at": "2026-10-14T10:00:00Z",
      "doorstep_otp_expires_at": "2026-10-15T10:00:00Z"
    }
    ```
  - `400 Bad Request`: OTP format invalid (not 6 digits) or incorrect OTP code (`attempts_left` decremented).
    ```json
    {
      "error": "Invalid verification code.",
      "attempts_left": 2
    }
    ```
  - `403 Forbidden`: Authenticated driver is not assigned to this delivery task.
  - `409 Conflict`:
    - `{"code": "payment_required"}`: Online payment has not been captured for an application with `payment_method='online'`. Consumes no OTP attempts.
    - `{"code": "payment_method_required"}`: Payment method is unset for an application with `total_amount > 0`. Consumes no OTP attempts.
    - `{"detail": "Cannot verify pickup for delivery in '<status>' state."}`: Delivery is not in `assigned` status (e.g. already picked up or failed).
  - `410 Gone`: Pickup OTP has expired. Reissue required via shelter.
  - `423 Locked`: Maximum verification attempts (3) exceeded.
  - `429 Too Many Requests`: Rate limit throttled.

---

### 4. Verify Doorstep OTP with Adopter
- **URL**: `POST /api/v1/deliveries/{id}/verify-doorstep/` (Legacy alias: `POST /api/v1/deliveries/{id}/verify-otp/`, `POST /api/deliveries/{id}/verify-otp/`)
- **Authentication**: Required (`Bearer <access_token>`)
- **Permissions**: Delivery Driver assigned to this specific delivery
- **Rate Limit**: 10 requests / minute
- **Request Body**:
  ```json
  {
    "otp": "123456"
  }
  ```
- **Responses**:
  - `200 OK`: Verification successful. Delivery transitioned to `delivered`.
    ```json
    {
      "message": "Doorstep delivery verified successfully.",
      "status": "delivered",
      "delivered_at": "2026-10-14T11:30:00Z"
    }
    ```
    *(Idempotent: Subsequent calls on already `delivered` tasks return `200 OK` with `{"message": "Delivery already completed."}`).*
  - `400 Bad Request`: OTP format invalid or incorrect code with attempts remaining.
  - `403 Forbidden`: Driver not assigned to this delivery.
  - `409 Conflict`:
    - `{"code": "cash_not_collected"}`: When `payment_method == 'cod'`, `total_amount > 0`, and `cash_collected < total_amount`. Consumes NO OTP verification attempts.
    - Delivery is not in `out_for_delivery` status.
  - `410 Gone`: Doorstep OTP has expired.
  - `423 Locked`: Maximum attempts (3) exceeded.
  - `429 Too Many Requests`: Rate limit throttled.

---

### 4b. COD Cash Collection
- **URL**: `POST /api/v1/delivery/tasks/{id}/collect-cash/` (Canonical), `POST /api/deliveries/{id}/collect-cash/` (Legacy alias)
- **Authentication**: Required (`Bearer <access_token>`)
- **Permissions**: Delivery Driver assigned to this specific delivery task
- **Rate Limit**: 10 requests / minute (`otp_verify` scope)
- **Request Body**:
  ```json
  {
    "amount": "150.00"
  }
  ```
- **Responses**:
  - `200 OK`: Cash collected successfully or already collected in full (idempotent, records at most one Payment ledger row).
    ```json
    {
      "message": "Cash collected successfully.",
      "cash_collected": "150.00",
      "remaining_amount": "0.00",
      "payment_status": "paid",
      "payment_id": "89304958-3920-4aef-b3e1-0c5a2c421f10"
    }
    ```
  - `400 Bad Request`:
    - Invalid decimal amount format or amount <= 0.
    - Submitted amount does not equal remaining balance: returns `{"detail": "...", "remaining_amount": "150.00"}`.
    - Payment method is not `'cod'`.
    - Total amount is 0.
  - `403 Forbidden`: Authenticated driver is not assigned to this delivery task.
  - `409 Conflict`: Delivery status is not `out_for_delivery`.

---

### 5. Shelter Reissue OTP Endpoint
- **URL**: `POST /api/v1/shelter/deliveries/{id}/reissue-otp/` (Legacy alias: `POST /api/deliveries/{id}/reissue-otp/`)
- **Authentication**: Required (`Bearer <access_token>`)
- **Permissions**: `IsVerifiedShelter` required (role `shelter` with `shelter_profile.is_verified=True`, matching origin shelter, or platform administrator)
- **Request Body**:
  ```json
  {
    "purpose": "pickup"
  }
  ```
  *(Supported purposes: `"pickup"` or `"doorstep"`).*
- **Responses**:
  - `200 OK (purpose="pickup")`:
    ```json
    {
      "message": "Pickup OTP reissued successfully.",
      "purpose": "pickup",
      "pickup_otp": "789123",
      "expires_at": "2026-10-15T12:00:00Z"
    }
    ```
  - `200 OK (purpose="doorstep")`:
    - If email dispatch succeeds:
      ```json
      {
        "detail": "Doorstep OTP reissued and emailed to adopter.",
        "message": "Doorstep OTP reissued and emailed to adopter.",
        "purpose": "doorstep",
        "email_dispatched": true,
        "expires_at": "2026-10-15T12:00:00Z"
      }
      ```
    - If email dispatch fails (e.g. SMTP/network failure):
      ```json
      {
        "detail": "Doorstep OTP reissued (email delivery failed).",
        "message": "Doorstep OTP reissued (email delivery failed).",
        "purpose": "doorstep",
        "email_dispatched": false,
        "expires_at": "2026-10-15T12:00:00Z"
      }
      ```
  - `400 Bad Request`: Invalid purpose (must be `"pickup"` or `"doorstep"`).
  - `403 Forbidden`: User is not associated with the managing shelter.
  - `409 Conflict`: State invariant violation (e.g. attempting pickup reissue when already out for delivery, or doorstep reissue while still in assigned status).

---

### 6. Driver Manual Status Update
- **URL**: `POST /api/v1/deliveries/{id}/status/` (Legacy alias: `POST /api/deliveries/{id}/status/`)
- **Authentication**: Required (`Bearer <access_token>`)
- **Permissions**: Assigned Driver
- **Request Body**:
  ```json
  {
    "status": "failed",
    "failure_reason": "Customer unavailable at address after repeated contact."
  }
  ```
- **Responses**:
  - `200 OK`: Status updated to `failed`, driver marked available (`is_available=True`). Note: marking a delivery as failed leaves the pet in `'pending'` and the application in `'approved'`.
  - `400 Bad Request`: Any status other than `"failed"` is rejected. Intermediate transitions (`out_for_delivery`, `delivered`) MUST use cryptographic OTP endpoints.
  - `409 Conflict`:
    - Delivery is not in `assigned` or `out_for_delivery` state.
    - Cash has already been collected (`cash_collected > 0`): returns `{"code": "cash_already_collected", "detail": "Cannot mark delivery as failed after cash has been collected. Please contact the shelter."}`.

---

### 7. Delivery Tracking (Customer / Shelter / Driver)
- **URL**: `GET /api/v1/delivery/tasks/{id}/tracking/` (Canonical), `GET /api/deliveries/{id}/track/` (Legacy alias)
- **Authentication**: Required (`Bearer <access_token>`)
- **Permissions**: `IsDeliveryParticipant` required (strictly restricted to the adoption applicant/customer, assigned driver, origin shelter, or platform administrators)
- **Responses**:
  - `200 OK`:
    ```json
    {
      "delivery": {
        "public_id": "5fa85f64-5717-4562-b3fc-2c963f66afa8",
        "status": "out_for_delivery",
        "driver_name": "John Driver",
        "driver_phone": "+919876543210",
        "vehicle_number": "KA-01-AB-1234",
        "doorstep_otp_sent_at": "2026-10-14T10:00:00Z",
        "doorstep_otp_expires_at": "2026-10-15T10:00:00Z"
      },
      "current_coordinates": {
        "latitude": "12.950000",
        "longitude": "77.660000",
        "recorded_at": "2026-10-14T10:15:00Z"
      }
    }
    ```
    *(Note: Plaintext OTP is never returned).*

---

## HTTP Status Code Summary Matrix

| Status Code | Meaning | Endpoint Scenarios |
|---|---|---|
| `200 OK` | Success / Idempotent | Verification succeeded, OTP reissued, or repeat verification on completed task. |
| `201 Created` | Resource Created | Delivery task assigned, breadcrumb ping saved. |
| `400 Bad Request` | Client Error | Malformed payload, non-6-digit OTP regex failure, invalid status transition, incorrect OTP with attempts remaining. |
| `403 Forbidden` | Access Denied | Authenticated user is not the assigned driver or authorized shelter profile. |
| `404 Not Found` | Resource Missing | Delivery task or driver does not exist. |
| `409 Conflict` | Invalid Lifecycle State | Attempting doorstep verification before pickup, or reissuing OTP in mismatched delivery status. |
| `410 Gone` | OTP Expired | The submitted OTP has passed its configured TTL expiry. |
| `423 Locked` | Security Lockout | Verification attempts exceeded (`OTP_MAX_ATTEMPTS = 3`). |
| `429 Too Many Requests` | Rate Limited | Exceeded `otp_verify` throttle rate (10 requests/min). |

---

## Breaking Changes from Legacy Behavior

1. **Serializer Schema Changes**:
   - `pickup_otp` removed from `DeliveryDriverTaskSerializer` and `ShelterDeliverySerializer`.
   - `doorstep_otp` removed from `ShelterDeliverySerializer`, `DeliveryCustomerSerializer`, and tracking endpoints.
   - Introduced read-only timestamp fields: `doorstep_otp_sent_at` and `doorstep_otp_expires_at`.
2. **Doorstep OTP Generation Timing**:
   - In legacy code, doorstep OTP was generated at task assignment.
   - In hardened architecture, doorstep OTP is generated **only** when the driver successfully verifies pickup at the shelter.
3. **Status Update Restrictions**:
   - `POST /deliveries/<id>/status/` no longer accepts `out_for_delivery` or `delivered`. Transitions can only occur via OTP verification endpoints or explicit failure declaration (`failed`).

---

## 8. Payment Processing Endpoints (Razorpay)

### 8.1 Create Razorpay Payment Order
- **URL**: `POST /api/v1/payments/create-order/`
- **Authentication**: Required (`Bearer <access_token>`)
- **Permissions**: IsAuthenticated (Adoption applicant only)
- **Security & Business Rules**:
  - **Ownership Check**: Authenticated user must equal `delivery.application.applicant`. Non-owner returns `404 Not Found`.
  - **Server-Side Paise Calculation**: Any client-supplied amount is strictly ignored. Order amount is derived server-side via `round(Decimal(application.total_amount) * 100)`.
  - **Precondition Validations**:
    - `total_amount <= 0`: Returns `400 Bad Request`.
    - `application.status != 'approved'`: Returns `409 Conflict`.
    - `application.payment_method == 'cod'`: Returns `409 Conflict`.
    - `application.payment_status == 'paid'`: Returns `409 Conflict`.
    - `delivery.status` not in `('assigned', 'out_for_delivery')`: Returns `409 Conflict`.
  - **Concurrency & Idempotent Order Reuse**: Row locked via `select_for_update()`. If an unpaid order (`created` or `pending`) already exists for this delivery with the identical amount, that order is returned without calling Razorpay or creating another `Payment` record.
  - **Payment Method Transition**: Automatically sets `application.payment_method = 'online'` if currently `'unset'` or `'online'`.
- **Request Body**:
  ```json
  {
    "delivery_public_id": "5fa85f64-5717-4562-b3fc-2c963f66afa8"
  }
  ```
- **Responses**:
  - `201 Created`:
    ```json
    {
      "id": 12,
      "public_id": "8fa85f64-5717-4562-b3fc-2c963f66afa9",
      "order_id": "order_EKfLsu8Gfb4M",
      "razorpay_order_id": "order_EKfLsu8Gfb4M",
      "amount_in_paise": 25000,
      "currency": "INR",
      "status": "created",
      "key_id": "rzp_test_mock_key_id",
      "created_at": "2026-10-15T10:00:00Z"
    }
    ```
  - `400 Bad Request`: `total_amount` is zero or negative.
  - `404 Not Found`: Delivery not found or authenticated user is not the applicant.
  - `409 Conflict`: Precondition unmet (unapproved, COD, already paid, invalid delivery state).

---

### 8.2 Verify Razorpay Payment Signature
- **URL**: `POST /api/v1/payments/verify/`
- **Authentication**: Required (`Bearer <access_token>`)
- **Permissions**: IsAuthenticated (Adoption applicant only)
- **Security & Business Rules**:
  - **Ownership Check**: Looked up under `select_for_update()`. If order does not exist or `payment.application.applicant != request.user`, returns `404 Not Found`.
  - **Idempotency**: If `payment.status` is already `'captured'` or `'paid'`, returns `200 OK` immediately without re-verification or side-effects.
  - **Amount Guard**: Verifies `payment.amount_in_paise` matches `round(Decimal(application.total_amount) * 100)`. Mismatch returns `400 Bad Request`.
  - **Signature Verification**: Validates HMAC-SHA256 signature against `settings.RAZORPAY_KEY_SECRET`. Invalid signature returns `400 Bad Request` with **no state change** to the Payment row.
  - **Unified State Capture**: Uses `mark_payment_captured`: transitions payment to `'captured'`, sets `application.payment_status = 'paid'` and `delivery.payment_status = 'paid'`.
- **Request Body**:
  ```json
  {
    "razorpay_order_id": "order_EKfLsu8Gfb4M",
    "razorpay_payment_id": "pay_29QQoUBcxrhErF",
    "razorpay_signature": "9ef4b61fa2a0ab365314275f0a8274718cb6c428787c8230230c120bc76eb4be"
  }
  ```
- **Responses**:
  - `200 OK`: Payment verified and captured.
  - `400 Bad Request`: Invalid signature or amount mismatch. No state change occurs.
  - `404 Not Found`: Unknown order or non-owner applicant.

---

### 8.3 Razorpay Webhook Processing
- **URL**: `POST /api/v1/payments/webhook/`
- **Authentication**: Public (`AllowAny`, `authentication_classes = []`). Zero Authorization header required.
- **Security & Business Rules**:
  - **Signature Verification**: Verified against `settings.RAZORPAY_WEBHOOK_SECRET` over raw `request.body`. Missing or invalid signature returns `400 Bad Request`.
  - **Safe Error Reporting**: Internal exceptions are never returned in response payload (logged privately; generic messages returned).
  - **`payment.captured`**:
    - Validates entity `amount` and `currency` against the Payment row. Mismatch logs a warning and leaves the record unchanged (`200 OK` ignored).
    - Idempotent: repeated webhook events produce no duplicate side-effects.
    - Transitions payment to `'captured'`, `application.payment_status = 'paid'`, `delivery.payment_status = 'paid'`.
  - **`payment.failed`**:
    - Never overwrites an already captured payment.
  - **Unknown Orders**:
    - Returns `200 OK` ignored with no changes.
- **Headers**:
  `X-Razorpay-Signature: <hmac_sha256_hex>`
- **Responses**:
  - `200 OK`: Event processed or safely ignored.
  - `400 Bad Request`: Invalid signature or malformed payload.

### 1.1 Cancel Delivery Task
- **URL**: `POST /api/v1/shelter/deliveries/{id}/cancel/` (Legacy alias: `POST /api/deliveries/{id}/cancel/`)
- **Authentication**: Required (`Bearer <access_token>`)
- **Permissions**: `IsVerifiedShelter` required (role `shelter` with `shelter_profile.is_verified=True` owning the delivery, or platform administrator)
- **Rate Limit**: 20 requests / minute (`shelter_cancel`)
- **Preconditions & Rules**:
  - Allowed strictly when `status in ('assigned', 'out_for_delivery')` AND `cash_collected == 0` AND no captured online payment exists for the application.
  - If `cash_collected > 0`: Returns HTTP `409 Conflict` `{"code": "cash_collected", "detail": "Cannot cancel delivery after cash has been collected. Please reconcile with driver."}`.
  - If a captured online payment exists (`Payment.status in ('captured', 'paid')`): Returns HTTP `409 Conflict` `{"code": "refund_required", "detail": "Cannot cancel delivery: online payment has already been captured. A refund must be processed before cancellation."}`.
  - Idempotent: If delivery is already in `'failed'` status, returns HTTP `200 OK` with `{"idempotent": true}` without side effects.
  - State Transitions upon cancellation:
    - `delivery.status` transitions to `'failed'`.
    - `driver.is_available` is reset to `True` (driver freed).
    - `pet.status` remains `'pending'`.
    - `application.status` remains `'approved'`.
    - In-app notification and email dispatched to the adopter via `transaction.on_commit`.
- **Responses**:
  - `200 OK`: Delivery cancelled successfully (or already failed).
    ```json
    {
      "detail": "Delivery cancelled successfully.",
      "delivery_id": "5fa85f64-5717-4562-b3fc-2c963f66afa8",
      "status": "failed",
      "idempotent": false
    }
    ```
  - `400 Bad Request`: Delivery is in an un-cancellable state (e.g. `delivered`).
  - `403 Forbidden`: Requesting user is not a verified shelter, or delivery belongs to a different shelter (`{"code": "shelter_not_verified"}` or `{"detail": "Delivery belongs to a different shelter."}`).
  - `409 Conflict`:
    - Cash has already been collected by driver (`{"code": "cash_collected"}`).
    - Online payment has already been captured for this application (`{"code": "refund_required"}`).

---

## Operational Notes

1. **SECRET_KEY Dependency & Key Rotation**:
   - OTP hashes are computed using HMAC-SHA256 keyed by Django's `settings.SECRET_KEY`.
   - Rotating `SECRET_KEY` in production invalidates all outstanding active OTP hashes.
   - If `SECRET_KEY` is rotated while deliveries are in-flight, affected deliveries must have their OTPs reissued via the shelter reissue endpoint (`POST /api/v1/shelter/deliveries/<id>/reissue-otp/`).
2. **Single-Response Plaintext Delivery**:
   - Plaintext pickup OTPs are returned strictly once in the response body of task assignment (`POST /api/v1/shelter/deliveries/assign/`) and pickup reissue (`POST /api/v1/shelter/deliveries/<id>/reissue-otp/`).
   - Plaintext OTPs are never persisted in the database or exposed via GET endpoints, model serializers, or application logs.


