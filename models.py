from datetime import date, datetime, timedelta, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import LargeBinary
from sqlalchemy.dialects.mysql import LONGBLOB
from sqlalchemy.orm import declared_attr

db = SQLAlchemy()

PHOTO_TYPE = LargeBinary().with_variant(LONGBLOB(), "mysql")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def age_from(dob, today=None):
    if not dob:
        return None
    today = today or date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # admin | doctor | nurse | reception
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    failed_attempts = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctors.id"))
    nurse_id = db.Column(db.Integer, db.ForeignKey("nurses.id"))
    doctor = db.relationship("Doctor", foreign_keys=[doctor_id])
    nurse = db.relationship("Nurse", foreign_keys=[nurse_id])


class SignupRequest(db.Model):
    __tablename__ = "signup_requests"
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(64), nullable=False)
    email = db.Column(db.String(120))
    password_hash = db.Column(db.String(255), nullable=False)
    requested_role = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="pending")  # pending|approved|rejected
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)


class _Staff:
    """Shared columns for doctors and nurses."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    date_of_birth = db.Column(db.Date, nullable=False)
    joined_on = db.Column(db.Date, nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120))
    availability = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    has_photo = db.Column(db.Boolean, nullable=False, default=False)
    photo_mime = db.Column(db.String(30))

    @declared_attr
    def photo_data(cls):
        # deferred: the image bytes are only loaded when the photo is actually served
        return db.deferred(db.Column(PHOTO_TYPE))

    @property
    def age(self):
        return age_from(self.date_of_birth)


class Doctor(_Staff, db.Model):
    __tablename__ = "doctors"
    doctor_code = db.Column(db.String(20), unique=True)
    specialty = db.Column(db.String(100), nullable=False)

    @property
    def code(self):
        return self.doctor_code


class Nurse(_Staff, db.Model):
    __tablename__ = "nurses"
    nurse_code = db.Column(db.String(20), unique=True)

    @property
    def code(self):
        return self.nurse_code


class Patient(db.Model):
    __tablename__ = "patients"
    id = db.Column(db.Integer, primary_key=True)
    patient_code = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(120), nullable=False)
    date_of_birth = db.Column(db.Date, nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    address = db.Column(db.Text, nullable=False)
    emergency_contact = db.Column(db.String(150))
    registered_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    visits = db.relationship("Visit", back_populates="patient",
                             order_by="desc(Visit.visit_date), desc(Visit.id)")

    @property
    def code(self):
        return self.patient_code

    @property
    def age(self):
        return age_from(self.date_of_birth)

    @property
    def visit_count(self):
        return len(self.visits)


class Visit(db.Model):
    __tablename__ = "visits"
    id = db.Column(db.Integer, primary_key=True)
    visit_code = db.Column(db.String(20), unique=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False, index=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctors.id"), nullable=False, index=True)
    nurse_id = db.Column(db.Integer, db.ForeignKey("nurses.id"), index=True)
    visit_type = db.Column(db.String(12), nullable=False, default="Outpatient")
    visit_date = db.Column(db.Date, nullable=False, default=date.today)
    reason = db.Column(db.Text, nullable=False)
    diagnosis = db.Column(db.Text)
    notes = db.Column(db.Text)
    nursing_notes = db.Column(db.Text)
    follow_up_date = db.Column(db.Date)
    discharge_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    patient = db.relationship("Patient", back_populates="visits")
    doctor = db.relationship("Doctor")
    nurse = db.relationship("Nurse")
    medicines = db.relationship("VisitMedicine", back_populates="visit", cascade="all, delete-orphan")

    @property
    def code(self):
        return self.visit_code


class Medicine(db.Model):
    """One row per batch/lot, so every batch has its own unique ID."""
    __tablename__ = "medicines"
    id = db.Column(db.Integer, primary_key=True)
    medicine_code = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(150), nullable=False, index=True)
    strength_form = db.Column(db.String(100))
    manufacturer = db.Column(db.String(150))
    supplier = db.Column(db.String(150))
    batch_no = db.Column(db.String(60), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    unit = db.Column(db.String(20), nullable=False, default="tablet")
    manufacture_date = db.Column(db.Date)
    expiry_date = db.Column(db.Date, nullable=False)
    purchase_price = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    selling_price = db.Column(db.Numeric(12, 2))
    reorder_threshold = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def code(self):
        return self.medicine_code

    @property
    def is_expired(self):
        return self.expiry_date < date.today()

    def expires_within(self, days):
        today = date.today()
        return today <= self.expiry_date <= today + timedelta(days=days)

    @property
    def is_low(self):
        return self.quantity <= self.reorder_threshold


class VisitMedicine(db.Model):
    __tablename__ = "visit_medicines"
    id = db.Column(db.Integer, primary_key=True)
    visit_id = db.Column(db.Integer, db.ForeignKey("visits.id"), nullable=False, index=True)
    medicine_id = db.Column(db.Integer, db.ForeignKey("medicines.id"), nullable=False)
    dosage = db.Column(db.String(60))
    frequency = db.Column(db.String(60))
    duration = db.Column(db.String(60))
    quantity = db.Column(db.Integer, nullable=False)
    instructions = db.Column(db.String(255))
    visit = db.relationship("Visit", back_populates="medicines")
    medicine = db.relationship("Medicine")


class Bill(db.Model):
    __tablename__ = "bills"
    id = db.Column(db.Integer, primary_key=True)
    bill_number = db.Column(db.String(30), unique=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False, index=True)
    visit_id = db.Column(db.Integer, db.ForeignKey("visits.id"))
    subtotal = db.Column(db.Numeric(12, 2), nullable=False)
    discount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    tax = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    total = db.Column(db.Numeric(12, 2), nullable=False)
    amount_paid = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    balance = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="unpaid")  # unpaid|partial|paid|cancelled|refunded
    status_reason = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    patient = db.relationship("Patient")
    visit = db.relationship("Visit")
    items = db.relationship("BillItem", back_populates="bill", cascade="all, delete-orphan")
    payments = db.relationship("Payment", back_populates="bill", order_by="Payment.id")

    @property
    def is_open(self):
        return self.status in ("unpaid", "partial")


class BillItem(db.Model):
    __tablename__ = "bill_items"
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("bills.id"), nullable=False, index=True)
    description = db.Column(db.String(200), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False)
    line_total = db.Column(db.Numeric(12, 2), nullable=False)
    bill = db.relationship("Bill", back_populates="items")


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("bills.id"), nullable=False, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    method = db.Column(db.String(20), nullable=False)
    reference_no = db.Column(db.String(80))
    paid_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    received_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    bill = db.relationship("Bill", back_populates="payments")
    received_by = db.relationship("User")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    username = db.Column(db.String(64))
    action = db.Column(db.String(60), nullable=False)
    entity_type = db.Column(db.String(30))
    entity_id = db.Column(db.String(30))
    timestamp = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    details = db.Column(db.Text)  # short, non-clinical JSON
