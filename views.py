import json
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template,
                   request, send_file, session, url_for)
from sqlalchemy import func, or_
from werkzeug.security import check_password_hash, generate_password_hash

from models import (AuditLog, Bill, BillItem, Doctor, Medicine, Nurse, Patient, Payment,
                    SignupRequest, User, Visit, VisitMedicine, db, utcnow)
from barcode import code128_svg
from pdfs import bill_pdf, patient_report_pdf
from security import (ALL_ROLES, audit, can, login_required, password_problem, require, safe_next)

main = Blueprint("main", __name__)
PHONE_RE = re.compile(r"^[0-9+\-\s()]{7,20}$")
PAY_METHODS = ("Cash", "UPI", "Card", "Bank transfer", "Insurance", "Other")


# ---- helpers --------------------------------------------------------------
class FormError(Exception):
    pass


def text(name, label, required=True, maxlen=255):
    v = (request.form.get(name) or "").strip()
    if not v:
        if required:
            raise FormError("%s is required." % label)
        return None
    if len(v) > maxlen:
        raise FormError("%s is too long (max %d characters)." % (label, maxlen))
    return v


def dt(name, label, required=True, past_only=False):
    v = (request.form.get(name) or "").strip()
    if not v:
        if required:
            raise FormError("%s is required." % label)
        return None
    try:
        d = datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        raise FormError("%s is not a valid date." % label)
    if past_only and d > date.today():
        raise FormError("%s cannot be in the future." % label)
    return d


def phone(name="phone", label="Phone number", required=True):
    v = text(name, label, required, 20)
    if v and not PHONE_RE.match(v):
        raise FormError("%s looks invalid. Use digits, spaces, + or - only." % label)
    return v


def email(name="email", required=False):
    v = text(name, "Email address", required, 120)
    if v and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
        raise FormError("Email address looks invalid.")
    return v.lower() if v else None


def money(value, label="Amount", allow_zero=True):
    try:
        d = Decimal(str(value).strip() or "0").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise FormError("%s is not a valid amount." % label)
    if d < 0 or (d == 0 and not allow_zero):
        raise FormError("%s must be %s." % (label, "zero or more" if allow_zero else "greater than zero"))
    if d > Decimal("9999999.99"):
        raise FormError("%s is too large." % label)
    return d


def whole(value, label, minimum=0, maximum=1000000):
    try:
        n = int(str(value).strip())
    except ValueError:
        raise FormError("%s must be a whole number." % label)
    if n < minimum or n > maximum:
        raise FormError("%s must be between %d and %d." % (label, minimum, maximum))
    return n


def assign_code(obj, field, prefix, width=4, with_year=False):
    """Codes derive from the primary key, so they are unique and never reused."""
    db.session.flush()
    mid = "%d-" % date.today().year if with_year else ""
    setattr(obj, field, "%s-%s%0*d" % (prefix, mid, width, obj.id))


def changed(obj, **new):
    return sorted(k for k, v in new.items() if getattr(obj, k) != v)


def commit_or_flash(message="Could not save. Please check the values and try again."):
    try:
        db.session.commit()
        return True
    except Exception:  # database errors must not leak details to the browser
        db.session.rollback()
        current_app.logger.exception("Database commit failed")
        flash(message, "error")
        return False


PHOTO_MAX_BYTES = 1024 * 1024  # 1 MB
_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))


def read_photo():
    """Return (bytes, mime) for an uploaded photo, None if none sent. Checks real file signature."""
    f = request.files.get("photo")
    if not f or not f.filename:
        return None
    data = f.read(PHOTO_MAX_BYTES + 1)
    if len(data) > PHOTO_MAX_BYTES:
        raise FormError("Photo is too large. Maximum size is 1 MB.")
    for sig, mime in _MAGIC:
        if data.startswith(sig):
            return data, mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data, "image/webp"
    raise FormError("Photo must be a JPEG, PNG or WebP image.")


def patient_scope(query):
    u = g.user
    if u.role == "doctor":
        sub = db.session.query(Visit.patient_id).filter(Visit.doctor_id == (u.doctor_id or -1))
        return query.filter(Patient.id.in_(sub))
    if u.role == "nurse":
        sub = db.session.query(Visit.patient_id).filter(Visit.nurse_id == (u.nurse_id or -1))
        return query.filter(Patient.id.in_(sub))
    return query


def visit_allowed(v):
    u = g.user
    if u.role == "doctor":
        return v.doctor_id == u.doctor_id
    if u.role == "nurse":
        return v.nurse_id == u.nurse_id
    return True


def get_patient(pid):
    return patient_scope(Patient.query.filter(Patient.id == pid)).first_or_404()


def get_visit(vid):
    v = db.session.get(Visit, vid) or abort(404)
    if not visit_allowed(v):
        abort(404)
    return v


# ---- health / auth --------------------------------------------------------
@main.route("/healthz")
def healthz():
    return "ok"


@main.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()[:64]
        password = request.form.get("password") or ""
        user = User.query.filter(func.lower(User.username) == username.lower()).first() if username else None
        generic = "Username or password is incorrect."
        now = utcnow()
        if user and user.locked_until and user.locked_until > now:
            audit("login.locked", "user", user.id)
            db.session.commit()
            flash("Too many failed attempts. Try again in a few minutes.", "error")
        elif not user:
            check_password_hash(generate_password_hash("dummy-password-1"), password)  # even out timing
            audit("login.failed", username=username[:64])
            db.session.commit()
            flash(generic, "error")
        elif not check_password_hash(user.password_hash, password):
            user.failed_attempts += 1
            if user.failed_attempts >= current_app.config["MAX_FAILED_LOGINS"]:
                user.locked_until = now + timedelta(minutes=current_app.config["LOCKOUT_MINUTES"])
                user.failed_attempts = 0
            audit("login.failed", "user", user.id)
            db.session.commit()
            flash(generic, "error")
        elif not user.is_active:
            audit("login.inactive", "user", user.id)
            db.session.commit()
            flash("This account is deactivated. Contact the administrator.", "error")
        else:
            user.failed_attempts, user.locked_until, user.last_login_at = 0, None, now
            session.clear()
            session["uid"] = user.id
            session.permanent = True
            g.user = user
            audit("login.success", "user", user.id)
            db.session.commit()
            return redirect(safe_next(request.args.get("next")) or url_for("main.dashboard"))
    return render_template("login.html")


@main.route("/logout", methods=["POST"])
@login_required
def logout():
    audit("logout", "user", g.user.id)
    db.session.commit()
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("main.login"))


@main.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        cur, new, again = (request.form.get(k) or "" for k in ("current", "new", "again"))
        problem = password_problem(new)
        if not check_password_hash(g.user.password_hash, cur):
            flash("Current password is incorrect.", "error")
        elif new != again:
            flash("New passwords do not match.", "error")
        elif problem:
            flash(problem, "error")
        elif check_password_hash(g.user.password_hash, new):
            flash("Choose a password different from the current one.", "error")
        else:
            g.user.password_hash = generate_password_hash(new)
            g.user.must_change_password = False
            audit("password.changed", "user", g.user.id)
            if commit_or_flash():
                flash("Password changed.", "success")
                return redirect(url_for("main.dashboard"))
    return render_template("change_password.html")


@main.route("/signup-request", methods=["GET", "POST"])
def signup_request():
    if not current_app.config["ENABLE_SIGNUP_REQUESTS"]:
        abort(404)
    if request.method == "POST":
        try:
            name = text("full_name", "Full name", maxlen=120)
            username = text("username", "Username", maxlen=64).lower()
            if not re.match(r"^[a-z0-9._-]{3,64}$", username):
                raise FormError("Username may use letters, numbers, dot, dash and underscore (3+ characters).")
            mail = email()
            role = request.form.get("role")
            if role not in ("doctor", "nurse", "reception"):
                raise FormError("Choose a role.")
            pw = request.form.get("password") or ""
            if password_problem(pw):
                raise FormError(password_problem(pw))
            if User.query.filter(func.lower(User.username) == username).first() or \
               SignupRequest.query.filter_by(username=username, status="pending").first():
                raise FormError("That username is not available.")
            db.session.add(SignupRequest(full_name=name, username=username, email=mail, requested_role=role,
                                         password_hash=generate_password_hash(pw)))
            audit("signup.requested", "signup_request", username, username=username)
            if commit_or_flash():
                flash("Request sent. You can sign in after an administrator approves it.", "success")
                return redirect(url_for("main.login"))
        except FormError as e:
            flash(str(e), "error")
    return render_template("signup_request.html")


# ---- dashboard ------------------------------------------------------------
@main.route("/")
@login_required
def dashboard():
    u, today = g.user, date.today()
    warn = current_app.config["EXPIRY_WARNING_DAYS"]
    stats = []
    if u.role == "admin":
        stats += [("Active doctors", Doctor.query.filter_by(is_active=True).count(), "main.staff_list", {"kind": "doctors"}),
                  ("Active nurses", Nurse.query.filter_by(is_active=True).count(), "main.staff_list", {"kind": "nurses"}),
                  ("Patients", Patient.query.count(), "main.patients", {})]
    if u.role in ("admin", "reception"):
        stats += [("Visits today", Visit.query.filter_by(visit_date=today).count(), "main.patients", {}),
                  ("Bills with balance", Bill.query.filter(Bill.status.in_(("unpaid", "partial"))).count(), "main.bills", {"status": "open"})]
    if u.role == "doctor":
        stats += [("My visits today", Visit.query.filter_by(visit_date=today, doctor_id=u.doctor_id or -1).count(), "main.patients", {}),
                  ("My follow-ups due (7 days)", Visit.query.filter(Visit.doctor_id == (u.doctor_id or -1), Visit.follow_up_date.between(today, today + timedelta(days=7))).count(), "main.patients", {})]
    if u.role == "nurse":
        stats += [("My visits today", Visit.query.filter_by(visit_date=today, nurse_id=u.nurse_id or -1).count(), "main.patients", {})]
    if can("medicines.view"):
        stats += [("Low stock batches", Medicine.query.filter(Medicine.is_active.is_(True), Medicine.quantity <= Medicine.reorder_threshold).count(), "main.medicines", {"filter": "low"}),
                  ("Expiring in %d days" % warn, Medicine.query.filter(Medicine.is_active.is_(True), Medicine.expiry_date.between(today, today + timedelta(days=warn))).count(), "main.medicines", {"filter": "expiring"}),
                  ("Expired batches", Medicine.query.filter(Medicine.is_active.is_(True), Medicine.expiry_date < today).count(), "main.medicines", {"filter": "expired"})]
    return render_template("dashboard.html", stats=stats)


# ---- doctors & nurses -----------------------------------------------------
STAFF = {"doctors": (Doctor, "DOC", "doctor_code", "doctors.view"),
         "nurses": (Nurse, "NUR", "nurse_code", "nurses.view")}


def staff_meta(kind):
    if kind not in STAFF:
        abort(404)
    model, prefix, code_field, perm = STAFF[kind]
    if not can(perm):
        abort(403)
    return model, prefix, code_field


@main.route("/staff/<kind>")
@login_required
def staff_list(kind):
    model, _, code_field = staff_meta(kind)
    q = (request.args.get("q") or "").strip()
    query = model.query
    if q:
        query = query.filter(or_(func.upper(getattr(model, code_field)) == q.upper(), model.name.like("%" + q + "%")))
    rows = query.order_by(model.name).all()
    if q and not rows:
        flash("No %s found for “%s”." % (kind[:-1], q), "info")
    return render_template("staff_list.html", kind=kind, rows=rows, q=q)


@main.route("/staff/<kind>/new", methods=["GET", "POST"])
@main.route("/staff/<kind>/<int:sid>/edit", methods=["GET", "POST"])
@require("staff.manage")
def staff_form(kind, sid=None):
    model, prefix, code_field = staff_meta(kind)
    obj = (db.session.get(model, sid) or abort(404)) if sid else None
    if request.method == "POST":
        try:
            data = dict(name=text("name", "Full name", maxlen=120), date_of_birth=dt("date_of_birth", "Date of birth", past_only=True),
                        joined_on=dt("joined_on", "Date of joining"), phone=phone(), email=email(),
                        availability=text("availability", "Availability", False, 255))
            if kind == "doctors":
                data["specialty"] = text("specialty", "Specialty", maxlen=100)
            photo = read_photo()
            if obj:
                audit("%s.updated" % kind[:-1], kind[:-1], obj.id, fields=changed(obj, **data) + (["photo"] if photo else []))
                for k, v in data.items():
                    setattr(obj, k, v)
            else:
                obj = model(**data)
                db.session.add(obj)
                assign_code(obj, code_field, prefix)
                audit("%s.created" % kind[:-1], kind[:-1], obj.id)
            if photo:
                obj.photo_data, obj.photo_mime, obj.has_photo = photo[0], photo[1], True
            elif request.form.get("remove_photo") and obj.has_photo:
                obj.photo_data, obj.photo_mime, obj.has_photo = None, None, False
                audit("%s.photo_removed" % kind[:-1], kind[:-1], obj.id)
            if commit_or_flash():
                flash("%s saved (%s)." % (kind[:-1].capitalize(), getattr(obj, code_field)), "success")
                return redirect(url_for("main.staff_list", kind=kind))
        except FormError as e:
            flash(str(e), "error")
    return render_template("staff_form.html", kind=kind, obj=obj)


@main.route("/staff/<kind>/<int:sid>/toggle", methods=["POST"])
@require("staff.manage")
def staff_toggle(kind, sid):
    model, _, _ = staff_meta(kind)
    obj = db.session.get(model, sid) or abort(404)
    obj.is_active = not obj.is_active
    audit("%s.%s" % (kind[:-1], "activated" if obj.is_active else "deactivated"), kind[:-1], obj.id)
    if commit_or_flash():
        flash("%s %s." % (obj.name, "activated" if obj.is_active else "deactivated"), "success")
    return redirect(url_for("main.staff_list", kind=kind))


@main.route("/staff/<kind>/<int:sid>/photo")
@login_required
def staff_photo(kind, sid):
    model, _, _ = staff_meta(kind)  # applies the same view permission as the staff list
    obj = db.session.get(model, sid) or abort(404)
    if not obj.has_photo or not obj.photo_data:
        abort(404)
    resp = Response(obj.photo_data, mimetype=obj.photo_mime or "application/octet-stream")
    resp.headers["Cache-Control"] = "private, max-age=300"
    resp.headers["Content-Disposition"] = "inline"
    return resp


@main.route("/patients/<int:pid>/barcode.svg")
@require("patients.view")
def patient_barcode(pid):
    p = get_patient(pid)
    resp = Response(code128_svg(p.patient_code), mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


# ---- patients -------------------------------------------------------------
@main.route("/patients")
@require("patients.view")
def patients():
    q = (request.args.get("q") or "").strip()
    query = patient_scope(Patient.query)
    if q:
        query = query.filter(or_(func.upper(Patient.patient_code) == q.upper(), Patient.name.like("%" + q + "%"), Patient.phone.like("%" + q + "%")))
    rows = query.order_by(Patient.id.desc()).limit(200).all()
    if q:
        exact = [p for p in rows if p.patient_code.upper() == q.upper()]
        if exact:
            return redirect(url_for("main.patient_detail", pid=exact[0].id))
        if not rows:
            flash("No patient found for “%s”. Check the patient ID and try again." % q, "info")
    return render_template("patients.html", rows=rows, q=q)


@main.route("/patients/new", methods=["GET", "POST"])
@main.route("/patients/<int:pid>/edit", methods=["GET", "POST"])
@require("patients.write")
def patient_form(pid=None):
    obj = get_patient(pid) if pid else None
    if request.method == "POST":
        try:
            data = dict(name=text("name", "Full name", maxlen=120), date_of_birth=dt("date_of_birth", "Date of birth", past_only=True),
                        phone=phone(), address=text("address", "Address / native place", maxlen=500),
                        emergency_contact=text("emergency_contact", "Emergency contact", False, 150))
            if obj:
                audit("patient.updated", "patient", obj.id, fields=changed(obj, **data))
                for k, v in data.items():
                    setattr(obj, k, v)
            else:
                obj = Patient(**data)
                db.session.add(obj)
                assign_code(obj, "patient_code", "PAT", 6)
                audit("patient.created", "patient", obj.id)
            if commit_or_flash():
                flash("Patient saved. Patient ID: %s" % obj.patient_code, "success")
                return redirect(url_for("main.patient_detail", pid=obj.id))
        except FormError as e:
            flash(str(e), "error")
    return render_template("patient_form.html", obj=obj)


@main.route("/patients/<int:pid>")
@require("patients.view")
def patient_detail(pid):
    p = get_patient(pid)
    audit("patient.viewed", "patient", p.id)
    db.session.commit()
    visits = [v for v in p.visits if visit_allowed(v)]
    return render_template("patient_detail.html", p=p, visits=visits)


@main.route("/patients/<int:pid>/toggle", methods=["POST"])
@require("patients.write")
def patient_toggle(pid):
    p = get_patient(pid)
    p.is_active = not p.is_active
    audit("patient.activated" if p.is_active else "patient.deactivated", "patient", p.id)
    commit_or_flash()
    return redirect(url_for("main.patient_detail", pid=p.id))


@main.route("/patients/<int:pid>/report.pdf")
@require("reports.patient")
def patient_report(pid):
    p = get_patient(pid)
    visits = [v for v in p.visits if visit_allowed(v)]
    pdf = patient_report_pdf(current_app.config, p, visits, include_clinical=can("clinical.view"))
    audit("report.downloaded", "patient", p.id)
    db.session.commit()
    return send_file(BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name="patient-report-%s.pdf" % p.patient_code)


@main.route("/reports")
@require("reports.patient")
def reports():
    q = (request.args.get("q") or "").strip()
    found = None
    if q:
        found = patient_scope(Patient.query.filter(func.upper(Patient.patient_code) == q.upper())).first()
        if not found:
            flash("No patient found with ID “%s”." % q, "info")
    return render_template("reports.html", q=q, found=found)


# ---- visits ---------------------------------------------------------------
@main.route("/visits/new", methods=["GET", "POST"])
@require("visits.schedule")
def visit_new():
    pid = request.values.get("patient_id", type=int)
    patient = db.session.get(Patient, pid) or abort(404)
    if request.method == "POST":
        try:
            doctor = db.session.get(Doctor, whole(request.form.get("doctor_id", ""), "Doctor", 1, 10**9))
            if not doctor or not doctor.is_active:
                raise FormError("Choose an active doctor.")
            nurse = None
            if request.form.get("nurse_id"):
                nurse = db.session.get(Nurse, whole(request.form["nurse_id"], "Nurse", 1, 10**9))
                if not nurse or not nurse.is_active:
                    raise FormError("Choose an active nurse.")
            vtype = request.form.get("visit_type")
            if vtype not in ("Outpatient", "Inpatient"):
                raise FormError("Choose a visit type.")
            v = Visit(patient_id=patient.id, doctor_id=doctor.id, nurse_id=nurse.id if nurse else None, visit_type=vtype,
                      visit_date=dt("visit_date", "Visit date"), reason=text("reason", "Reason for visit", maxlen=1000),
                      follow_up_date=dt("follow_up_date", "Follow-up date", False), created_by_id=g.user.id)
            db.session.add(v)
            assign_code(v, "visit_code", "VIS", 6)
            audit("visit.created", "visit", v.id, patient=patient.patient_code)
            if commit_or_flash():
                flash("Visit %s recorded." % v.visit_code, "success")
                return redirect(url_for("main.visit_detail", vid=v.id))
        except FormError as e:
            flash(str(e), "error")
    return render_template("visit_form.html", patient=patient, obj=None, **_visit_choices())


def _visit_choices():
    return dict(doctors=Doctor.query.filter_by(is_active=True).order_by(Doctor.name).all(),
                nurses=Nurse.query.filter_by(is_active=True).order_by(Nurse.name).all())


@main.route("/visits/<int:vid>")
@require("patients.view")
def visit_detail(vid):
    v = get_visit(vid)
    meds = []
    if can("visits.clinical") and current_app.config["ENABLE_DOCTOR_PRESCRIBING"]:
        meds = Medicine.query.filter(Medicine.is_active.is_(True), Medicine.quantity > 0, Medicine.expiry_date >= date.today()).order_by(Medicine.name).all()
    return render_template("visit_detail.html", v=v, available_meds=meds, **_visit_choices())


@main.route("/visits/<int:vid>/schedule", methods=["POST"])
@require("visits.schedule")
def visit_schedule(vid):
    v = get_visit(vid)
    try:
        doctor = db.session.get(Doctor, whole(request.form.get("doctor_id", ""), "Doctor", 1, 10**9)) or abort(404)
        nurse = db.session.get(Nurse, int(request.form["nurse_id"])) if request.form.get("nurse_id") else None
        data = dict(doctor_id=doctor.id, nurse_id=nurse.id if nurse else None, reason=text("reason", "Reason for visit", maxlen=1000),
                    visit_date=dt("visit_date", "Visit date"), follow_up_date=dt("follow_up_date", "Follow-up date", False),
                    discharge_date=dt("discharge_date", "Discharge date", False))
        audit("visit.updated", "visit", v.id, fields=changed(v, **data))
        for k, val in data.items():
            setattr(v, k, val)
        if commit_or_flash():
            flash("Visit updated.", "success")
    except (FormError, ValueError) as e:
        flash(str(e) if isinstance(e, FormError) else "Invalid selection.", "error")
    return redirect(url_for("main.visit_detail", vid=v.id))


@main.route("/visits/<int:vid>/clinical", methods=["POST"])
@require("visits.clinical")
def visit_clinical(vid):
    v = get_visit(vid)
    try:
        v.diagnosis = text("diagnosis", "Diagnosis", False, 2000)
        v.notes = text("notes", "Clinical notes", False, 5000)
        v.follow_up_date = dt("follow_up_date", "Follow-up date", False)
        v.discharge_date = dt("discharge_date", "Discharge date", False)
        audit("visit.clinical_updated", "visit", v.id)
        if commit_or_flash():
            flash("Clinical details saved.", "success")
    except FormError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("main.visit_detail", vid=v.id))


@main.route("/visits/<int:vid>/nursing", methods=["POST"])
@require("visits.nursing")
def visit_nursing(vid):
    if not current_app.config["ENABLE_NURSE_NOTES"]:
        abort(403)
    v = get_visit(vid)
    try:
        v.nursing_notes = text("nursing_notes", "Nursing notes", False, 5000)
        audit("visit.nursing_updated", "visit", v.id)
        if commit_or_flash():
            flash("Nursing notes saved.", "success")
    except FormError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("main.visit_detail", vid=v.id))


@main.route("/visits/<int:vid>/medicines", methods=["POST"])
@require("visits.clinical")
def visit_add_medicine(vid):
    if not current_app.config["ENABLE_DOCTOR_PRESCRIBING"]:
        abort(403)
    v = get_visit(vid)
    try:
        qty = whole(request.form.get("quantity", ""), "Quantity", 1, 100000)
        med = Medicine.query.filter_by(id=whole(request.form.get("medicine_id", ""), "Medicine", 1, 10**9)).with_for_update().first()
        if not med or not med.is_active:
            raise FormError("Choose a medicine from the list.")
        if med.is_expired:
            raise FormError("%s (batch %s) is expired and cannot be dispensed." % (med.name, med.batch_no))
        if med.quantity < qty:
            raise FormError("Only %d %s of %s left in this batch." % (med.quantity, med.unit, med.name))
        med.quantity -= qty
        db.session.add(VisitMedicine(visit_id=v.id, medicine_id=med.id, quantity=qty, dosage=text("dosage", "Dosage", False, 60),
                                      frequency=text("frequency", "Frequency", False, 60), duration=text("duration", "Duration", False, 60),
                                      instructions=text("instructions", "Instructions", False, 255)))
        audit("visit.medicine_added", "visit", v.id, medicine=med.medicine_code, qty=qty)
        if commit_or_flash():
            flash("Medicine added and stock updated.", "success")
    except FormError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("main.visit_detail", vid=v.id))


@main.route("/visits/<int:vid>/medicines/<int:vmid>/remove", methods=["POST"])
@require("visits.clinical")
def visit_remove_medicine(vid, vmid):
    v = get_visit(vid)
    vm = VisitMedicine.query.filter_by(id=vmid, visit_id=v.id).first_or_404()
    med = Medicine.query.filter_by(id=vm.medicine_id).with_for_update().first()
    med.quantity += vm.quantity
    audit("visit.medicine_removed", "visit", v.id, medicine=med.medicine_code, qty=vm.quantity)
    db.session.delete(vm)
    if commit_or_flash():
        flash("Medicine removed and stock restored.", "success")
    return redirect(url_for("main.visit_detail", vid=v.id))


# ---- medicines ------------------------------------------------------------
@main.route("/medicines")
@require("medicines.view")
def medicines():
    flt = request.args.get("filter", "")
    q = (request.args.get("q") or "").strip()
    today, warn = date.today(), current_app.config["EXPIRY_WARNING_DAYS"]
    query = Medicine.query
    if q:
        query = query.filter(or_(func.upper(Medicine.medicine_code) == q.upper(), Medicine.name.like("%" + q + "%"), Medicine.batch_no.like("%" + q + "%")))
    if flt == "low":
        query = query.filter(Medicine.is_active.is_(True), Medicine.quantity <= Medicine.reorder_threshold)
    elif flt == "expiring":
        query = query.filter(Medicine.is_active.is_(True), Medicine.expiry_date.between(today, today + timedelta(days=warn)))
    elif flt == "expired":
        query = query.filter(Medicine.is_active.is_(True), Medicine.expiry_date < today)
    return render_template("medicines.html", rows=query.order_by(Medicine.expiry_date).all(), flt=flt, q=q, warn=warn)


@main.route("/medicines/new", methods=["GET", "POST"])
@main.route("/medicines/<int:mid>/edit", methods=["GET", "POST"])
@require("medicines.manage")
def medicine_form(mid=None):
    obj = (db.session.get(Medicine, mid) or abort(404)) if mid else None
    if request.method == "POST":
        try:
            data = dict(name=text("name", "Medicine name", maxlen=150), strength_form=text("strength_form", "Strength / form", False, 100),
                        manufacturer=text("manufacturer", "Manufacturer", False, 150), supplier=text("supplier", "Supplier", False, 150),
                        batch_no=text("batch_no", "Batch / lot number", maxlen=60), unit=text("unit", "Unit", maxlen=20),
                        manufacture_date=dt("manufacture_date", "Manufacturing date", False), expiry_date=dt("expiry_date", "Expiry date"),
                        purchase_price=money(request.form.get("purchase_price"), "Purchase price"),
                        selling_price=money(request.form.get("selling_price"), "Selling price") if request.form.get("selling_price", "").strip() else None,
                        reorder_threshold=whole(request.form.get("reorder_threshold", "0"), "Reorder threshold"))
            if data["manufacture_date"] and data["manufacture_date"] > data["expiry_date"]:
                raise FormError("Expiry date must be after the manufacturing date.")
            if obj:
                audit("medicine.updated", "medicine", obj.id, fields=changed(obj, **data))
                for k, v in data.items():
                    setattr(obj, k, v)
            else:
                obj = Medicine(quantity=whole(request.form.get("quantity", "0"), "Quantity"), **data)
                db.session.add(obj)
                assign_code(obj, "medicine_code", "MED", 5)
                audit("medicine.created", "medicine", obj.id, qty=obj.quantity)
            if commit_or_flash():
                flash("Medicine batch saved (%s)." % obj.medicine_code, "success")
                return redirect(url_for("main.medicines"))
        except FormError as e:
            flash(str(e), "error")
    return render_template("medicine_form.html", obj=obj)


@main.route("/medicines/<int:mid>/adjust", methods=["POST"])
@require("medicines.manage")
def medicine_adjust(mid):
    med = Medicine.query.filter_by(id=mid).with_for_update().first_or_404()
    try:
        delta = whole(request.form.get("delta", ""), "Adjustment", -1000000, 1000000)
        reason = text("reason", "Reason", maxlen=200)
        if med.quantity + delta < 0:
            raise FormError("Stock cannot go below zero (current: %d)." % med.quantity)
        before, med.quantity = med.quantity, med.quantity + delta
        audit("medicine.stock_adjusted", "medicine", med.id, before=before, after=med.quantity, reason=reason)
        if commit_or_flash():
            flash("Stock updated: %d → %d." % (before, med.quantity), "success")
    except FormError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("main.medicines"))


@main.route("/medicines/<int:mid>/toggle", methods=["POST"])
@require("medicines.manage")
def medicine_toggle(mid):
    med = db.session.get(Medicine, mid) or abort(404)
    med.is_active = not med.is_active
    audit("medicine.activated" if med.is_active else "medicine.deactivated", "medicine", med.id)
    commit_or_flash()
    return redirect(url_for("main.medicines"))


# ---- billing --------------------------------------------------------------
@main.route("/bills")
@require("billing.view")
def bills():
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status", "")
    query = Bill.query.join(Patient, Bill.patient_id == Patient.id)
    if q:
        query = query.filter(or_(func.upper(Bill.bill_number) == q.upper(), func.upper(Patient.patient_code) == q.upper(), Patient.name.like("%" + q + "%")))
    if status == "open":
        query = query.filter(Bill.status.in_(("unpaid", "partial")))
    elif status in ("paid", "cancelled", "refunded"):
        query = query.filter(Bill.status == status)
    rows = query.order_by(Bill.id.desc()).limit(200).all()
    if q and not rows:
        flash("No bill found for “%s”." % q, "info")
    return render_template("bills.html", rows=rows, q=q, status=status)


@main.route("/bills/new", methods=["GET", "POST"])
@require("billing.write")
def bill_new():
    code = (request.values.get("patient") or "").strip()
    patient = Patient.query.filter(func.upper(Patient.patient_code) == code.upper()).first() if code else None
    if code and not patient:
        flash("No patient found with ID “%s”." % code, "info")
    if request.method == "POST" and patient:
        try:
            visit = None
            if request.form.get("visit_id"):
                visit = Visit.query.filter_by(id=whole(request.form["visit_id"], "Visit", 1, 10**9), patient_id=patient.id).first()
                if not visit:
                    raise FormError("Choose one of this patient's visits.")
            items = []
            for d, qn, pr in zip(request.form.getlist("desc"), request.form.getlist("qty"), request.form.getlist("price")):
                if not (d.strip() or qn.strip() or pr.strip()):
                    continue
                if not d.strip():
                    raise FormError("Every bill line needs a description.")
                n = whole(qn, "Quantity", 1, 100000)
                unit = money(pr, "Unit price")
                items.append((d.strip()[:200], n, unit, (unit * n).quantize(Decimal("0.01"))))
            if not items:
                raise FormError("Add at least one bill line.")
            subtotal = sum((i[3] for i in items), Decimal("0.00"))
            discount = money(request.form.get("discount") or "0", "Discount")
            if discount > subtotal:
                raise FormError("Discount cannot be more than the subtotal.")
            try:
                rate = Decimal((request.form.get("tax_percent") or "0").strip() or "0")
            except InvalidOperation:
                raise FormError("Tax percent is not a valid number.")
            if rate < 0 or rate > 100:
                raise FormError("Tax percent must be between 0 and 100.")
            tax = ((subtotal - discount) * rate / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            total = subtotal - discount + tax
            bill = Bill(patient_id=patient.id, visit_id=visit.id if visit else None, subtotal=subtotal, discount=discount,
                        tax=tax, total=total, amount_paid=Decimal("0.00"), balance=total, status="unpaid", created_by_id=g.user.id)
            for d, n, unit, line in items:
                bill.items.append(BillItem(description=d, quantity=n, unit_price=unit, line_total=line))
            db.session.add(bill)
            assign_code(bill, "bill_number", "BILL", 6, with_year=True)
            audit("bill.created", "bill", bill.id, total=total)
            if commit_or_flash():
                flash("Bill %s created." % bill.bill_number, "success")
                return redirect(url_for("main.bill_detail", bid=bill.id))
        except FormError as e:
            flash(str(e), "error")
    return render_template("bill_new.html", patient=patient, code=code)


@main.route("/bills/<int:bid>")
@require("billing.view")
def bill_detail(bid):
    return render_template("bill_detail.html", b=db.session.get(Bill, bid) or abort(404), methods=PAY_METHODS)


def _locked_bill(bid):
    return Bill.query.filter_by(id=bid).with_for_update().first_or_404()


@main.route("/bills/<int:bid>/pay", methods=["POST"])
@require("billing.write")
def bill_pay(bid):
    b = _locked_bill(bid)
    try:
        if not b.is_open:
            raise FormError("This bill is %s and cannot take payments." % b.status)
        amount = money(request.form.get("amount"), "Payment amount", allow_zero=False)
        if amount > b.balance:
            raise FormError("Payment is more than the balance due (%s)." % b.balance)
        method = request.form.get("method")
        if method not in PAY_METHODS:
            raise FormError("Choose a payment method.")
        ref = text("reference_no", "Reference number", False, 80)
        if method != "Cash" and not ref:
            raise FormError("Enter the transaction or reference number for non-cash payments.")
        db.session.add(Payment(bill_id=b.id, amount=amount, method=method, reference_no=ref, received_by_user_id=g.user.id))
        b.amount_paid += amount
        b.balance = b.total - b.amount_paid
        b.status = "paid" if b.balance == 0 else "partial"
        audit("payment.recorded", "bill", b.id, amount=amount, method=method, status=b.status)
        if commit_or_flash():
            flash("Payment of Rs. %s recorded." % amount, "success")
    except FormError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("main.bill_detail", bid=bid))


@main.route("/bills/<int:bid>/void", methods=["POST"])
@require("billing.void")
def bill_void(bid):
    """Cancel an unpaid bill or mark a bill with payments as refunded. Never deletes."""
    b = _locked_bill(bid)
    try:
        reason = text("reason", "Reason", maxlen=200)
        if b.status in ("cancelled", "refunded"):
            raise FormError("This bill is already %s." % b.status)
        new = "cancelled" if b.amount_paid == 0 else "refunded"
        audit("bill." + new, "bill", b.id, before=b.status, paid=b.amount_paid, reason=reason)
        b.status, b.status_reason, b.balance = new, reason, Decimal("0.00")
        if commit_or_flash():
            flash("Bill marked %s." % new, "success")
    except FormError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("main.bill_detail", bid=bid))


@main.route("/bills/<int:bid>/pdf")
@require("billing.view")
def bill_pdf_download(bid):
    b = db.session.get(Bill, bid) or abort(404)
    audit("bill.downloaded", "bill", b.id)
    db.session.commit()
    return send_file(BytesIO(bill_pdf(current_app.config, b)), mimetype="application/pdf", as_attachment=True,
                     download_name="bill-%s.pdf" % b.bill_number)


# ---- administration -------------------------------------------------------
def _staff_links():
    return dict(doctors=Doctor.query.filter_by(is_active=True).order_by(Doctor.name).all(),
                nurses=Nurse.query.filter_by(is_active=True).order_by(Nurse.name).all())


def _link_ids(role):
    did = request.form.get("doctor_id", type=int) if role == "doctor" else None
    nid = request.form.get("nurse_id", type=int) if role == "nurse" else None
    return did, nid


@main.route("/admin/users", methods=["GET", "POST"])
@require("users.manage")
def admin_users():
    if request.method == "POST":
        try:
            username = text("username", "Username", maxlen=64).lower()
            if not re.match(r"^[a-z0-9._-]{3,64}$", username):
                raise FormError("Username may use letters, numbers, dot, dash and underscore (3+ characters).")
            role = request.form.get("role")
            if role not in ALL_ROLES:
                raise FormError("Choose a role.")
            pw = request.form.get("password") or ""
            if password_problem(pw):
                raise FormError(password_problem(pw))
            if User.query.filter(func.lower(User.username) == username).first():
                raise FormError("That username already exists.")
            mail = email()
            if mail and User.query.filter_by(email=mail).first():
                raise FormError("That email is already used by another account.")
            did, nid = _link_ids(role)
            if role == "doctor" and not did:
                raise FormError("Link a doctor account to a doctor record.")
            if role == "nurse" and not nid:
                raise FormError("Link a nurse account to a nurse record.")
            u = User(username=username, email=mail, role=role, password_hash=generate_password_hash(pw),
                     must_change_password=True, doctor_id=did, nurse_id=nid)
            db.session.add(u)
            db.session.flush()
            audit("user.created", "user", u.id, role=role)
            if commit_or_flash():
                flash("Account %s created. They must change the password at first sign-in." % username, "success")
                return redirect(url_for("main.admin_users"))
        except FormError as e:
            flash(str(e), "error")
    return render_template("admin_users.html", users=User.query.order_by(User.username).all(), roles=ALL_ROLES, **_staff_links())


@main.route("/admin/users/<int:uid>/update", methods=["POST"])
@require("users.manage")
def admin_user_update(uid):
    u = db.session.get(User, uid) or abort(404)
    action = request.form.get("action")
    if u.id == g.user.id and action in ("toggle", "role"):
        flash("You cannot change your own role or status.", "error")
        return redirect(url_for("main.admin_users"))
    try:
        if action == "toggle":
            u.is_active = not u.is_active
            audit("user.activated" if u.is_active else "user.deactivated", "user", u.id)
        elif action == "role":
            role = request.form.get("role")
            if role not in ALL_ROLES:
                raise FormError("Choose a role.")
            did, nid = _link_ids(role)
            if (role == "doctor" and not did) or (role == "nurse" and not nid):
                raise FormError("Link this account to a doctor or nurse record.")
            audit("user.role_changed", "user", u.id, before=u.role, after=role)
            u.role, u.doctor_id, u.nurse_id = role, did, nid
        elif action == "reset":
            pw = request.form.get("password") or ""
            if password_problem(pw):
                raise FormError(password_problem(pw))
            u.password_hash, u.must_change_password = generate_password_hash(pw), True
            u.failed_attempts, u.locked_until = 0, None
            audit("user.password_reset", "user", u.id)
        elif action == "unlock":
            u.failed_attempts, u.locked_until = 0, None
            audit("user.unlocked", "user", u.id)
        else:
            abort(400)
        if commit_or_flash():
            flash("Account updated.", "success")
    except FormError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("main.admin_users"))


@main.route("/admin/signups", methods=["GET", "POST"])
@require("users.manage")
def admin_signups():
    if request.method == "POST":
        req = db.session.get(SignupRequest, request.form.get("id", type=int) or 0) or abort(404)
        if req.status != "pending":
            flash("That request was already reviewed.", "info")
        elif request.form.get("decision") == "approve":
            role = request.form.get("role")
            did, nid = _link_ids(role)
            if role not in ALL_ROLES or role == "admin" or (role == "doctor" and not did) or (role == "nurse" and not nid):
                flash("Choose a role and, for doctors and nurses, the matching staff record.", "error")
            elif User.query.filter(func.lower(User.username) == req.username).first():
                flash("That username now exists. Reject this request.", "error")
            else:
                u = User(username=req.username, email=req.email, role=role, password_hash=req.password_hash,
                         doctor_id=did, nurse_id=nid)
                db.session.add(u)
                req.status, req.reviewed_by_id, req.reviewed_at = "approved", g.user.id, utcnow()
                db.session.flush()
                audit("signup.approved", "user", u.id, role=role)
                if commit_or_flash():
                    flash("Approved. The account is active.", "success")
        else:
            req.status, req.reviewed_by_id, req.reviewed_at = "rejected", g.user.id, utcnow()
            audit("signup.rejected", "signup_request", req.id)
            if commit_or_flash():
                flash("Request rejected.", "success")
        return redirect(url_for("main.admin_signups"))
    rows = SignupRequest.query.order_by(SignupRequest.id.desc()).limit(100).all()
    return render_template("admin_signups.html", rows=rows, roles=[r for r in ALL_ROLES if r != "admin"], **_staff_links())


@main.route("/admin/audit")
@require("audit.view")
def audit_log():
    q = (request.args.get("q") or "").strip()
    query = AuditLog.query
    if q:
        query = query.filter(or_(AuditLog.username == q, AuditLog.action.like(q + "%"), AuditLog.entity_id == q))
    rows = query.order_by(AuditLog.id.desc()).limit(300).all()
    return render_template("audit.html", rows=rows, q=q)
