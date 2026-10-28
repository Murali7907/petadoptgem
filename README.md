# PetAdoptGem - Pet Adoption & Logistics Platform

[![CI](https://github.com/Murali7907/petadoptgem/actions/workflows/ci.yml/badge.svg)](https://github.com/Murali7907/petadoptgem/actions/workflows/ci.yml)

A modern, full-stack pet adoption management platform backend built with Django REST Framework, PostgreSQL, and PostGIS. The platform connects animal shelters, prospective pet adopters, and logistics delivery partners with real-time location pings, 6-digit OTP delivery handoffs, KYC verification, saved favorites, in-app notifications, and Razorpay payment processing.

---

## Key Features

- **Multi-Role Authentication & Access Control**
  - Custom JWT authentication with embedded role claims (`customer`, `shelter`, `delivery_partner`, `admin`).
  - Specialized profiles (`ShelterProfile`, `DeliveryProfile`).

- **Pet Discovery & Management**
  - Public pet catalog with search, filters, and PostGIS distance/proximity calculation.
  - Shelter pet management CRUD with multi-image uploads and medical record history.
  - Customer Saved Pets (`/api/v1/customer/favorites/`) for bookmarking pets.

- **Adoption Applications & KYC Workflow**
  - Application submissions with housing details, fenced yard flags, other pets disclosure, and document proof URLs.
  - Shelter review, status transitions (`pending`, `under_review`, `approved`, `rejected`), and drop-off delivery address capture.

- **Logistics & Doorstep Delivery**
  - Delivery dispatch, fleet driver assignment, and live HTML5 GPS location ping ingestion.
  - Doorstep 6-digit OTP verification for secure pet handoffs.

- **Payments & Dual Review System**
  - Razorpay order generation and HMAC-SHA256 webhook ingestion.
  - Post-delivery customer and shelter mutual review ratings.

- **In-App Notifications**
  - Notification stream with unread filters and batch mark-read endpoints.

- **API Documentation & Admin Oversight**
  - Interactive OpenAPI 3.0 Swagger documentation via `drf-spectacular` (`/api/docs/` and `/api/schema/`).
  - Platform analytics and shelter verification endpoints for administrators.

---

## Tech Stack

- **Backend Framework**: Django 6.1.2 (pinned in requirements.lock.txt), Django REST Framework
- **Database**: PostgreSQL 18 with PostGIS extensions
- **API Documentation**: drf-spectacular (OpenAPI 3.0 / Swagger UI)
- **Payment Processing**: Razorpay Python SDK
- **Image Processing**: Pillow

---

## Getting Started

### Prerequisites

- Python 3.10+
- PostgreSQL 14+ (with PostGIS extension enabled)
- Git

### Installation & Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/Murali7907/petadoptgem.git
   cd petadoptgem
   ```

2. **Navigate to the backend directory and set up a virtual environment**:
   ```bash
   cd backend
   python -m venv .venv
   # On Windows:
   .venv\Scripts\activate
   # On macOS/Linux:
   source .venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**:
   Create a `.env` file in the `backend/` directory (you can copy `.env.example`):
   ```bash
   cp .env.example .env
   ```
   Update `.env` with your PostgreSQL database credentials and keys:
   ```env
   DB_NAME=pet_adoption
   DB_USER=postgres
   DB_PASSWORD=your_password
   DB_HOST=127.0.0.1
   DB_PORT=5432
   SECRET_KEY=your-django-secret-key
   DEBUG=True
   RAZORPAY_KEY_ID=rzp_test_xxx
   RAZORPAY_KEY_SECRET=xxx
   RAZORPAY_WEBHOOK_SECRET=xxx
   ```

5. **Run migrations**:
   ```bash
   python manage.py migrate
   ```

6. **Seed the database (Optional)**:
   ```bash
   python manage.py seed_platform
   ```

7. **Run the development server**:
   ```bash
   python manage.py runserver
   ```

8. **Access Swagger API Documentation**:
   Visit [http://127.0.0.1:8000/api/docs/](http://127.0.0.1:8000/api/docs/) in your browser.

---

## Running Automated Tests

Run the test suite across all apps:
```bash
python manage.py test
```

---

## License

This project is licensed under the MIT License.
