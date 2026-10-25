import os
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _flag(name, default="0"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)
        return "sqlite:///" + os.path.join(BASE_DIR, "instance", "dev.db")
    if url.startswith("mysql://"):
        url = "mysql+pymysql://" + url[len("mysql://"):]
    return url


class Config:
    APP_ENV = os.environ.get("APP_ENV", "development").lower()
    IS_PRODUCTION = APP_ENV == "production"

    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    _engine = {"pool_pre_ping": True, "pool_recycle": 280}
    if SQLALCHEMY_DATABASE_URI.startswith("mysql"):
        _engine["pool_size"] = 5
        _engine["max_overflow"] = 5
        if not _flag("DB_SSL_DISABLED"):
            # TLS to TiDB Cloud, verifying the server certificate.
            _engine["connect_args"] = {
                "ssl": {"ca": os.environ.get("DB_SSL_CA", "/etc/ssl/certs/ca-certificates.crt")}
            }
    SQLALCHEMY_ENGINE_OPTIONS = _engine

    # Sessions / cookies
    SESSION_MINUTES = int(os.environ.get("SESSION_MINUTES", "30"))
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=SESSION_MINUTES)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = IS_PRODUCTION

    # Login protection
    MAX_FAILED_LOGINS = 5
    LOCKOUT_MINUTES = 15

    # Hospital identity (only shown when provided)
    HOSPITAL_NAME = "Thangam Hospital"
    HOSPITAL_ADDRESS = "6/17, Madurai Bypass, Tenkasi, Tamil Nadu"
    HOSPITAL_PHONE = os.environ.get("HOSPITAL_PHONE", "").strip()
    HOSPITAL_EMAIL = os.environ.get("HOSPITAL_EMAIL", "").strip()
    HOSPITAL_MAP_URL = os.environ.get("HOSPITAL_MAP_URL", "").strip()
    SOCIAL_LINKS = [
        (label, os.environ.get(var, "").strip())
        for label, var in (("Instagram", "SOCIAL_INSTAGRAM"), ("Facebook", "SOCIAL_FACEBOOK"), ("X", "SOCIAL_X"))
        if os.environ.get(var, "").strip().startswith("https://")
    ]

    # Feature switches
    ENABLE_SIGNUP_REQUESTS = _flag("ENABLE_SIGNUP_REQUESTS", "0")
    ENABLE_DOCTOR_PRESCRIBING = _flag("ENABLE_DOCTOR_PRESCRIBING", "1")
    ENABLE_NURSE_NOTES = _flag("ENABLE_NURSE_NOTES", "1")
    EXPIRY_WARNING_DAYS = int(os.environ.get("EXPIRY_WARNING_DAYS", "30"))
