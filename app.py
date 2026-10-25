"""Thangam Hospital Management System - Flask entry point.

Run locally:   flask --app app init-db && flask --app app run --debug
Production:    gunicorn app:app
"""
import os
import secrets
from datetime import date, datetime, timedelta

import click
from dotenv import load_dotenv
from flask import Flask, g, render_template, request, session
from werkzeug.security import generate_password_hash

load_dotenv()

from config import Config  # noqa: E402  (after load_dotenv so .env is honoured)
from models import (Doctor, Medicine, Nurse, Patient, User, Visit, age_from, db, utcnow)  # noqa: E402
from security import audit, can, check_csrf, csrf_token, password_problem  # noqa: E402
from views import main  # noqa: E402


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    if not app.config["SECRET_KEY"]:
        if app.config["IS_PRODUCTION"]:
            raise RuntimeError("SECRET_KEY must be set in production.")
        app.config["SECRET_KEY"] = "dev-only-insecure-key"

    db.init_app(app)
    app.register_blueprint(main)

    @app.before_request
    def load_user():
        g.user = None
        uid = session.get("uid")
        if uid:
            user = db.session.get(User, uid)
            if user and user.is_active:
                g.user = user
            else:
                session.clear()
        check_csrf()
        allowed = ("main.change_password", "main.logout", "main.healthz", "static")
        if g.user and g.user.must_change_password and request.endpoint not in allowed:
            from flask import flash, redirect, url_for
            flash("Set a new password to continue.", "info")
            return redirect(url_for("main.change_password"))

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'self'")
        if g.get("user") or request.endpoint in ("main.login",):
            resp.headers["Cache-Control"] = "no-store"  # keep patient pages out of shared caches
        if app.config["IS_PRODUCTION"]:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return resp

    @app.context_processor
    def inject():
        return dict(can=can, csrf_token=csrf_token, cfg=app.config, current_user=g.get("user"), today=date.today(), now_utc=utcnow())

    @app.template_filter("dmy")
    def dmy(value):
        return value.strftime("%d %b %Y") if value else "-"

    @app.template_filter("ist")
    def ist(value):
        """Stored UTC -> India Standard Time for display."""
        return (value + timedelta(hours=5, minutes=30)).strftime("%d %b %Y, %H:%M") if value else "-"

    @app.template_filter("inr")
    def inr(value):
        return "Rs. {:,.2f}".format(value or 0)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(500)
    def error_page(err):
        code = getattr(err, "code", 500)
        messages = {400: getattr(err, "description", "Bad request."), 403: "You do not have permission to open this page.",
                    404: "We could not find that page or record.", 500: "Something went wrong on our side. Nothing was saved."}
        if code == 500:
            db.session.rollback()
        return render_template("error.html", code=code, message=messages.get(code, "Error.")), code

    register_cli(app)
    return app


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create tables (safe to re-run) and bootstrap the first admin from env vars."""
        db.create_all()
        click.echo("Tables ready.")
        username = os.environ.get("ADMIN_BOOTSTRAP_USERNAME", "").strip().lower()
        password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD", "")
        if User.query.count() == 0 and username and password:
            if password_problem(password):
                raise click.ClickException("ADMIN_BOOTSTRAP_PASSWORD: " + password_problem(password))
            db.session.add(User(username=username, role="admin", password_hash=generate_password_hash(password),
                                must_change_password=True))
            db.session.commit()
            click.echo("First administrator '%s' created (must change password at first login)." % username)
        elif User.query.count() == 0:
            click.echo("No users yet. Run `flask --app app create-admin` or set ADMIN_BOOTSTRAP_* and re-run init-db.")

    @app.cli.command("add-photo-columns")
    def add_photo_columns():
        """Upgrade an EXISTING database: add photo columns to doctors and nurses (safe to re-run)."""
        from sqlalchemy import inspect, text
        insp = inspect(db.engine)
        blob = "LONGBLOB" if db.engine.dialect.name == "mysql" else "BLOB"
        for table in ("doctors", "nurses"):
            have = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in (("has_photo", "BOOLEAN NOT NULL DEFAULT 0"), ("photo_mime", "VARCHAR(30)"), ("photo_data", blob)):
                if col not in have:
                    db.session.execute(text("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl)))
                    click.echo("Added %s.%s" % (table, col))
        db.session.commit()
        click.echo("Photo columns ready.")

    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    @click.password_option()
    def create_admin(username, password):
        """Create an administrator interactively (password is stored hashed only)."""
        username = username.strip().lower()
        if password_problem(password):
            raise click.ClickException(password_problem(password))
        if User.query.filter_by(username=username).first():
            raise click.ClickException("Username already exists.")
        db.session.add(User(username=username, role="admin", password_hash=generate_password_hash(password), must_change_password=True))
        db.session.commit()
        click.echo("Administrator created.")

    @app.cli.command("seed-demo")
    @click.option("--force", is_flag=True, help="Allow in production (never do this with real data).")
    def seed_demo(force):
        """Insert DUMMY data for testing. Prints random passwords for demo accounts."""
        if app.config["IS_PRODUCTION"] and not force:
            raise click.ClickException("Refusing to seed demo data in production.")
        from views import assign_code
        if Doctor.query.count():
            raise click.ClickException("Data already present; demo seed skipped.")
        d = Doctor(name="Dr. Demo Physician", date_of_birth=date(1980, 5, 12), joined_on=date(2018, 6, 1), phone="9000000001",
                   specialty="General Medicine", availability="Mon-Sat 9:00-13:00")
        n = Nurse(name="Demo Nurse", date_of_birth=date(1990, 3, 3), joined_on=date(2020, 1, 10), phone="9000000002", availability="Day shift")
        db.session.add_all([d, n])
        assign_code(d, "doctor_code", "DOC")
        assign_code(n, "nurse_code", "NUR")
        p = Patient(name="Demo Patient", date_of_birth=date(1985, 8, 20), phone="9000000003", address="Dummy address, Tenkasi")
        db.session.add(p)
        assign_code(p, "patient_code", "PAT", 6)
        v = Visit(patient_id=p.id, doctor_id=d.id, nurse_id=n.id, reason="Fever for two days (demo)", visit_date=date.today())
        db.session.add(v)
        assign_code(v, "visit_code", "VIS", 6)
        for name, qty, days, thr in (("Paracetamol 500 mg", 200, 400, 50), ("Amoxicillin 250 mg", 20, 20, 30), ("Old Batch Syrup", 15, -10, 5)):
            m = Medicine(name=name, batch_no="DEMO-" + name[:3].upper(), quantity=qty, unit="unit", expiry_date=date.today() + timedelta(days=days),
                         purchase_price=2, selling_price=3, reorder_threshold=thr)
            db.session.add(m)
            assign_code(m, "medicine_code", "MED", 5)
        creds = []
        for username, role, kw in (("demo.doctor", "doctor", dict(doctor_id=d.id)), ("demo.nurse", "nurse", dict(nurse_id=n.id)),
                                   ("demo.reception", "reception", {})):
            pw = secrets.token_urlsafe(9) + "7a"
            db.session.add(User(username=username, role=role, password_hash=generate_password_hash(pw), **kw))
            creds.append((username, pw))
        db.session.commit()
        click.echo("Demo data created. Demo logins (shown once):")
        for u, pw in creds:
            click.echo("  %s  /  %s" % (u, pw))


app = create_app()

if __name__ == "__main__":
    app.run(debug=not Config.IS_PRODUCTION)
