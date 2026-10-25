"""Authentication helpers, role permissions, CSRF and audit logging."""
import hmac
import json
import secrets
from functools import wraps

from flask import abort, current_app, g, request, session

from models import AuditLog, db

ALL_ROLES = ("admin", "doctor", "nurse", "reception")

PERMISSIONS = {
    "staff.manage": {"admin"},
    "doctors.view": {"admin", "doctor", "nurse", "reception"},
    "nurses.view": {"admin", "doctor", "nurse"},
    "patients.view": {"admin", "doctor", "nurse", "reception"},
    "patients.write": {"admin", "reception"},
    "visits.schedule": {"admin", "reception"},
    "clinical.view": {"admin", "doctor", "nurse"},
    "visits.clinical": {"admin", "doctor"},
    "visits.nursing": {"admin", "nurse"},
    "medicines.view": {"admin", "doctor", "nurse"},
    "medicines.manage": {"admin"},
    "billing.view": {"admin", "reception"},
    "billing.write": {"admin", "reception"},
    "billing.void": {"admin"},
    "reports.patient": {"admin", "doctor"},
    "users.manage": {"admin"},
    "audit.view": {"admin"},
}


def can(permission, user=None):
    user = user or getattr(g, "user", None)
    return bool(user) and user.role in PERMISSIONS.get(permission, set())


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("user"):
            from flask import redirect, url_for
            return redirect(url_for("main.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def require(permission):
    """Server-side role check for every protected route."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not can(permission):
                audit("access.denied", "route", request.endpoint or "?")
                db.session.commit()
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


# ---- CSRF -----------------------------------------------------------------
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Your session expired or the form was invalid. Please go back and try again.")


# ---- Audit ----------------------------------------------------------------
def audit(action, entity_type=None, entity_id=None, **details):
    """Queue an audit row (committed with the caller's transaction).

    Keep `details` small and non-clinical: field names, amounts, status changes.
    """
    user = g.get("user")
    db.session.add(AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else details.pop("username", None),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        details=json.dumps(details, default=str)[:2000] if details else None,
    ))


# ---- Passwords ------------------------------------------------------------
def password_problem(pw):
    if len(pw) < 10:
        return "Password must be at least 10 characters."
    if not any(c.isalpha() for c in pw) or not any(c.isdigit() for c in pw):
        return "Password must contain both letters and numbers."
    return None


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None
