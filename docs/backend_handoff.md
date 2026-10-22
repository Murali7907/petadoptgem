# KindHeart Pet Adoption & Logistics — Backend Handoff Guide

This guide is the complete API handoff manual for frontend and mobile client developers integrating with the KindHeart Backend. It documents authentication lifecycles, role-based endpoint permissions, error codes, delivery state machines, and canonical workflows.

---

## 1. Authentication Lifecycle & Rate Throttling

KindHeart uses **JSON Web Tokens (SimpleJWT)** with embedded role claims and sliding expiration.

### 1.1 Token Endpoints

| Endpoint | Method | Purpose | Request Body | Response Payload |
|---|---|---|---|---|
| `/api/v1/auth/register/` | `POST` | User registration | `username`, `email`, `password`, `role`, `phone`, `address`, *(optional shelter profile fields)* | `UserRegistration` representation |
| `/api/v1/auth/login/` | `POST` | User login | `username`, `password` | `{"access": "<jwt>", "refresh": "<jwt>"}` |
| `/api/v1/auth/token/refresh/` | `POST` | Rotate access token | `{"refresh": "<jwt>"}` | `{"access": "<new_jwt>", "refresh": "<optional_rotated_jwt>"}` |
| `/api/v1/auth/token/verify/` | `POST` | Validate token validity | `{"token": "<jwt>"}` | `200 OK` or `401 Unauthorized` |
| `/api/v1/auth/logout/` | `POST` | Blacklist refresh token | `{"refresh": "<jwt>"}` | `{"message": "Successfully logged out."}` |

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
- **`otp_verify`**: `5 requests/min` (default). Configurable via `OTP_THROTTLE_RATE`.
- **`shelter_cancel`**: `20 requests/min` (default). Configurable via `SHELTER_CANCEL_THROTTLE_RATE`.

When throttled, the server returns HTTP `429 Too Many Requests` with a `Retry-After` header in seconds.

---

## 2. Role-Based Permissions Matrix

| Endpoint Route | HTTP Method | Allowed Roles | Required Permissions / Invariants |
|---|---|---|---|
| `/api/v1/pets/` | `GET` | Public | Open discovery with filters (`species`, `gender`, `breed`, `min_age`, `max_age`, `city`, `q`). |
| `/api/v1/pets/<uuid:public_id>/` | `GET` | Public | Public pet detail. `medical_overview` masks document links for non-applicants. |
| `/api/v1/customer/favorites/` | `GET`, `POST` | `customer` | Authenticated customer only. |
| `/api/v1/customer/favorites/<uuid:pet_public_id>/` | `DELETE` | `customer` | Authenticated customer only. |
| `/api/v1/applications/` | `GET`, `POST` | `customer` | Submit and list personal adoption applications. |
| `/api/v1/applications/<uuid:public_id>/` | `GET` | `customer` | View application details with distance and fees. |
| `/api/v1/applications/<uuid:public_id>/cancel/` | `POST` | `customer` | Blocked (409) if online payment captured or delivery cash collected. |
| `/api/v1/payments/create-order/` | `POST` | `customer` | Initiates Razorpay checkout for approved online adoption task. |
| `/api/v1/payments/verify/` | `POST` | `customer` | Verifies cryptographic HMAC-SHA256 Razorpay payment signature. |
| `/api/v1/customer/deliveries/<uuid:public_id>/track/` | `GET` | `customer` | Live delivery status, driver info, and driver coordinates. |
| `/api/v1/applications/<uuid:public_id>/review/` | `POST` | `customer` | Submit rating & review once delivery status is `delivered`. |
| `/api/v1/shelter/profile/` | `GET`, `PUT`, `PATCH` | `shelter` | Manage shelter organization profile. |
| `/api/v1/shelter/pets/` | `GET`, `POST` | `shelter` | `POST` requires `is_verified=True`. |
| `/api/v1/shelter/pets/<uuid:public_id>/` | `GET`, `PUT`, `PATCH`, `DELETE` | `shelter` | Writes require `is_verified=True` and ownership. |
| `/api/v1/pets/<uuid:public_id>/images/` | `POST` | `shelter` | Upload pet photos (max 10MB, JPEG/PNG/WebP, validated by Pillow). Requires `is_verified=True`. |
| `/api/v1/pets/<uuid:public_id>/medical-records/` | `POST` | `shelter` | Upload medical records & PDF/image certificates (max 15MB, `%PDF-` signature check). Requires `is_verified=True`. |
| `/api/v1/shelter/applications/` | `GET` | `shelter` | Filter applications for shelter pets. |
| `/api/v1/shelter/applications/<uuid:public_id>/` | `GET` | `shelter` | View application, proof documents, and notes. |
| `/api/v1/shelter/applications/<uuid:public_id>/review/` | `POST` | `shelter` | Move application to `reviewing`. Requires `is_verified=True`. |
| `/api/v1/shelter/applications/<uuid:public_id>/approve/` | `POST` | `shelter` | Approve application; sets pet to `pending`, auto-rejects competitors. Requires `is_verified=True`. |
| `/api/v1/shelter/applications/<uuid:public_id>/reject/` | `POST` | `shelter` | Reject application. Blocked (409) if online payment captured or delivery cash collected. Requires `is_verified=True`. |
| `/api/v1/shelter/deliveries/available-drivers/` | `GET` | `shelter` | Lists available active delivery drivers. |
| `/api/v1/shelter/deliveries/assign/` | `POST` | `shelter` | Assigns driver; creates delivery task; generates pickup OTP. Requires `is_verified=True`. |
| `/api/v1/shelter/deliveries/<uuid:public_id>/cancel/` | `POST` | `shelter` | Cancels delivery. Blocked (409) if online payment captured or delivery cash collected. Requires `is_verified=True`. |
| `/api/v1/shelter/reviews/` | `GET` | `shelter` | View customer ratings and feedback. |
| `/api/v1/delivery/profile/` | `GET`, `PATCH` | `delivery` | Driver profile management (toggle `is_available`). |
| `/api/v1/delivery/tasks/` | `GET` | `delivery` | List assigned and active delivery tasks. |
| `/api/v1/delivery/tasks/<uuid:public_id>/` | `GET` | `delivery` | Task details including addresses and customer contact. |
| `/api/v1/delivery/tasks/<uuid:public_id>/pickup/` | `POST` | `delivery` | Verifies pickup OTP; validates payment guard; transitions to `out_for_delivery`. |
| `/api/v1/delivery/tasks/<uuid:public_id>/collect-cash/` | `POST` | `delivery` | Driver records cash collected from customer for COD. |
| `/api/v1/delivery/tasks/<uuid:public_id>/verify-doorstep/` | `POST` | `delivery` | Verifies doorstep OTP; validates cash guard; transitions to `delivered`. |
| `/api/v1/delivery/tasks/<uuid:public_id>/status/` | `PATCH` | `delivery` | Driver reports delivery failure or status updates. |
| `/api/v1/delivery/tasks/<uuid:public_id>/location/` | `POST` | `delivery` | Updates driver's live GPS coordinates. |
| `/api/v1/delivery/reviews/` | `GET` | `delivery` | View driver rating and customer feedback. |
| `/api/v1/admin/stats/` | `GET` | `admin` | Aggregated platform metrics and KPIs. |
| `/api/v1/admin/shelters/` | `GET` | `admin` | List shelters with verification filter. |
| `/api/v1/admin/shelters/<uuid:public_id>/verify/` | `POST` | `admin` | Set `is_verified = True/False`. |
| `/api/v1/admin/drivers/` | `POST` | `admin` | Provision new delivery drivers. |

---

## 3. Structured Error Code Catalogue

All high-impact errors return structured JSON payloads with a standard `"code"` string for client-side programmatic branching:

```json
{
  "code": "<error_code>",
  "detail": "<human_readable_message>"
}
```

| HTTP Status | Error Code (`"code"`) | Occurring Endpoints | Meaning & Client Remediation |
|---|---|---|---|
| `400 Bad Request` | `invalid_token` | `/api/v1/auth/logout/` | The refresh token provided in the logout request is malformed, corrupted, or has an invalid signature. Prompt user to clear local storage. |
| `400 Bad Request` | `adopter_email_missing` | `/api/v1/shelter/deliveries/assign/` | Delivery task cannot be assigned because the applicant account has no email address. The shelter cannot dispatch the delivery until the customer adds an email address to receive the doorstep OTP. |
| `403 Forbidden` | `shelter_not_verified` | All shelter write endpoints | The shelter profile has `is_verified = False`. The platform administrator must verify the shelter license/profile in the admin dashboard before pets or deliveries can be managed. |
| `409 Conflict` | `payment_required` | `/api/v1/delivery/tasks/<id>/pickup/` | Pickup OTP verification was rejected because the application payment method is `'online'`, but no captured Razorpay payment exists. Driver must wait until the customer completes checkout. |
| `409 Conflict` | `payment_method_required` | `/api/v1/delivery/tasks/<id>/pickup/` | Pickup OTP verification was rejected because the application payment method is `'unset'` and `total_amount > 0`. Shelter or adopter must designate payment method before pet leaves the facility. |
| `409 Conflict` | `cash_not_collected` | `/api/v1/delivery/tasks/<id>/verify-doorstep/` | Doorstep OTP verification was rejected because the delivery is Cash on Delivery (COD) and the driver has not yet called `collect-cash` with `cash_collected >= total_amount`. Driver must collect cash first. |
| `409 Conflict` | `cash_already_collected` | `/api/v1/delivery/tasks/<id>/collect-cash/` | Driver attempted to record cash collection on a delivery where cash has already been logged. |
| `409 Conflict` | `cash_collected` | `/api/v1/shelter/deliveries/<id>/cancel/` | Shelter attempted to cancel a delivery task after the driver physically collected cash from the customer. The shelter must reconcile cash with the driver directly. |
| `409 Conflict` | `refund_required` | - `/api/v1/applications/<id>/cancel/`<br>- `/api/v1/shelter/applications/<id>/reject/`<br>- `/api/v1/shelter/deliveries/<id>/cancel/`<br>- `/api/v1/adoptions/<id>/status/` | An online Razorpay payment has already been captured (`PaymentStatus.CAPTURED`), or delivery cash has been collected. The application or delivery cannot be cancelled/rejected until shelter staff arranges a refund with the customer. |
| `423 Locked` | *(outcome code)* | `/api/v1/delivery/tasks/<id>/pickup/`<br>`/api/v1/delivery/tasks/<id>/verify-doorstep/` | Maximum failed OTP attempts exceeded (3 attempts). The OTP is permanently locked. Contact shelter staff to reissue an OTP. |
| `410 Gone` | *(outcome code)* | `/api/v1/delivery/tasks/<id>/pickup/`<br>`/api/v1/delivery/tasks/<id>/verify-doorstep/` | The OTP has expired (standard 24-hour TTL). Contact shelter staff to reissue. |

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
   - Track driver GPS coordinates in real-time via `GET /api/v1/customer/deliveries/<uuid:public_id>/track/`.
   - When the driver picks up the pet from the shelter, the adopter receives an email containing the 6-digit **Doorstep OTP**.
   - Upon arrival, give the Doorstep OTP (and cash if COD) to the driver.
6. **Submit Review**:
   - After delivery is completed, submit feedback via `POST /api/v1/applications/<uuid:public_id>/review/` with `{"pet_rating": 5, "driver_rating": 5, "review_text": "Wonderful experience!"}`.

---

### 5.2 Shelter Staff Happy Path
1. **Profile Setup & Verification**:
   - Register account with `role: "shelter"`. Admin approves shelter (`is_verified = True`).
   - Update address and coordinates: `PATCH /api/v1/shelter/profile/`.
2. **Pet Listing Management**:
   - Create pet: `POST /api/v1/shelter/pets/` with `{"name": "Buddy", "species": "dog", ...}`.
   - Upload pet photos: `POST /api/v1/pets/<public_id>/images/` (multipart file upload).
   - Upload vaccination / medical certs: `POST /api/v1/pets/<public_id>/medical-records/` (multipart PDF/image upload).
3. **Application Review & Decision**:
   - Check incoming applications: `GET /api/v1/shelter/applications/?status=submitted`.
   - Mark under review: `POST /api/v1/shelter/applications/<id>/review/`.
   - Approve application: `POST /api/v1/shelter/applications/<id>/approve/`.
     - *System Side-Effects*: Pet status moves to `'pending'`; all competing applications for this pet are automatically rejected.
4. **Driver Assignment & Pet Handover**:
   - Find active drivers: `GET /api/v1/shelter/deliveries/available-drivers/`.
   - Assign driver: `POST /api/v1/shelter/deliveries/assign/` with `{"application_public_id": "...", "driver_public_id": "...", "payment_method": "online"}`.
   - Note the 6-digit **Pickup OTP** from the response.
   - When the driver arrives at the shelter, provide the Pickup OTP after handing over the pet.

---

### 5.3 Delivery Driver Happy Path
1. **Go On-Duty**:
   - Set status to available: `PATCH /api/v1/delivery/profile/` with `{"is_available": true}`.
2. **Receive Delivery Task**:
   - Fetch assigned tasks: `GET /api/v1/delivery/tasks/`.
   - Inspect pickup shelter address and customer dropoff address: `GET /api/v1/delivery/tasks/<id>/`.
3. **Pickup from Shelter**:
   - Arrive at shelter facility. Obtain 6-digit Pickup OTP from shelter staff.
   - Call `POST /api/v1/delivery/tasks/<id>/pickup/` with `{"otp": "123456"}`.
   - Task transitions to `out_for_delivery`.
4. **Transit & GPS Updates**:
   - Periodically stream vehicle coordinates: `POST /api/v1/delivery/tasks/<id>/location/` with `{"latitude": 12.9716, "longitude": 77.5946}`.
5. **Doorstep Delivery & Handover**:
   - Arrive at customer address.
   - *If COD*: Collect cash payment and call `POST /api/v1/delivery/tasks/<id>/collect-cash/` with `{"amount_collected": "..."}`.
   - Request the 6-digit Doorstep OTP from the customer.
   - Call `POST /api/v1/delivery/tasks/<id>/verify-doorstep/` with `{"otp": "654321"}`.
   - Task transitions to `delivered`. Driver is automatically returned to available status (`is_available = True`).

---

*KindHeart Engineering Team — Production Backend API Specifications*
