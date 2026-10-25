# Thangam Hospital Management System

Flask + SQLAlchemy + TiDB Cloud (MySQL-compatible) + Render, built from the
*Corrected Requirements & Implementation Specification*.
Hospital: 6/17, Madurai Bypass, Tenkasi, Tamil Nadu.

> Use **dummy data only** until the hospital has approved real-world use and a
> qualified adviser has reviewed Indian health-data and privacy obligations.

## What is included

| Area | Implemented |
|---|---|
| Accounts | Admin-created accounts, 4 roles (admin, doctor, nurse, reception), hashed passwords, forced change of temporary passwords, 30-minute session timeout, lockout after 5 failed logins (15 min), optional admin-approved sign-up requests |
| Authorization | Every route checks the role on the server (`security.py` → `PERMISSIONS`). Doctors and nurses only see patients/visits assigned to them |
| Doctors / nurses | Unique IDs (DOC-0001, NUR-0001), search by ID or name, calculated age, availability, activate/deactivate |
| Patients & visits | Unique patient IDs (PAT-000001), each attendance is its own visit (VIS-000001), visit count is calculated, search by ID shows summary + history |
| Medicines | Batch-level records (MED-00001), low-stock and expiry warnings, expired batches cannot be dispensed, stock adjustments need a reason |
| Billing | Itemised bills (BILL-2026-000001), discount and tax, partial payments, payment history, cancel/refund with reason (never deleted), money stored as `DECIMAL` |
| Photos | Doctor and nurse photos (JPEG/PNG/WebP, max 1 MB, file signature checked) are stored in the database as binary data and served only to signed-in users with permission |
| Barcodes | Each patient ID is shown as a Code 128 barcode on the bill page, the bill PDF and the patient page |
| Reports | Patient report PDF and payment bill PDF (separate downloads), print-friendly pages |
| Security | CSRF tokens, secure cookies, CSP and other headers, parameterised ORM queries, audit log, no patient data in logs |

## Upgrading an existing database

If you created the tables before the photo feature was added, run once:
`flask --app app add-photo-columns` (adds the photo columns to `doctors` and `nurses`). New databases need nothing extra.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # edit SECRET_KEY at least
flask --app app init-db
flask --app app create-admin                          # prompts for username + password
flask --app app seed-demo                             # optional: dummy data + demo logins
flask --app app run --debug                           # http://127.0.0.1:5000
```

With no `DATABASE_URL`, a local SQLite file (`instance/dev.db`) is used for development only.

## Database: TiDB Cloud

1. Create a TiDB Cloud cluster and a database such as `thangam_hospital_db`.
2. Create a SQL user for the app (not root) with only the rights it needs on that database.
3. Copy host, port, user and password from TiDB's *Connect* dialog and build:
   `mysql+pymysql://USER:PASSWORD@HOST:4000/thangam_hospital_db`
   (URL-encode special characters in the password, e.g. `@` → `%40`).
4. TLS is on by default; `DB_SSL_CA` points to the system CA bundle
   (`/etc/ssl/certs/ca-certificates.crt` on Render).
5. Tables are created by `flask --app app init-db`. For later schema changes, adopt
   Flask-Migrate/Alembic before real data exists.

## Deploy on Render

1. Push this folder to a **private** GitHub repo (`.gitignore` already excludes secrets).
2. In Render choose **New → Blueprint** (uses `render.yaml`) or create a Web Service manually:
   - Build: `pip install -r requirements.txt && flask --app app init-db`
   - Start: `gunicorn app:app --workers 2 --timeout 60`
   - Health check path: `/healthz`
3. Set environment variables in Render (never in code): `DATABASE_URL`, `SECRET_KEY`,
   `APP_ENV=production`, and for the first deploy `ADMIN_BOOTSTRAP_USERNAME` /
   `ADMIN_BOOTSTRAP_PASSWORD` (≥10 chars, letters + numbers). The first admin must change the
   password at first sign-in; remove the bootstrap variables afterwards.
4. Optional: `HOSPITAL_PHONE`, `HOSPITAL_EMAIL`, `HOSPITAL_MAP_URL`, and verified
   `SOCIAL_INSTAGRAM` / `SOCIAL_FACEBOOK` / `SOCIAL_X` (https URLs). Nothing is shown until you set it.
5. Free Render services sleep when idle; use a paid plan for a real hospital.

## Roles at a glance

| | Admin | Doctor | Nurse | Reception |
|---|:-:|:-:|:-:|:-:|
| Staff records add/edit | ✔ | | | |
| View doctors / nurses | ✔ | ✔ / ✔ | ✔ / ✔ | ✔ / – |
| Register patients, schedule visits | ✔ | | | ✔ |
| View patients | all | assigned | assigned | all (no clinical notes) |
| Diagnosis, clinical notes, prescribe | ✔ | own visits | | |
| Nursing notes | ✔ | | own visits | |
| Medicines | manage | view | view | |
| Billing | ✔ (void/refund) | | | create, pay |
| Patient report PDF | ✔ | assigned | | |
| Accounts, audit log | ✔ | | | |

## Decisions taken where the spec left questions open (change as needed)

- Adding a medicine to a visit **dispenses it**: stock is reduced, and restored if removed.
- A visit is Outpatient or Inpatient; beds/wards, lab reports and appointments are not built.
- Passwords are reset by an administrator (no email service is configured).
- Refunds are recorded as a status with a reason, not as a refund payment ledger.
- Switches: `ENABLE_DOCTOR_PRESCRIBING`, `ENABLE_NURSE_NOTES`, `ENABLE_SIGNUP_REQUESTS`.

## Before go-live (from the spec's checklist)

Test backup **restore**, confirm HTTPS, review role permissions with the hospital, replace dummy data,
and confirm the open questions in section 11 of the specification.

## Layout

```
app.py        app factory, security headers, CLI (init-db, create-admin, seed-demo)
config.py     environment-driven settings          models.py   database tables
views.py      all routes                           security.py roles, CSRF, audit
pdfs.py       ReportLab PDFs                       templates/ static/   UI
render.yaml   Render blueprint                     .env.example         settings template
```
