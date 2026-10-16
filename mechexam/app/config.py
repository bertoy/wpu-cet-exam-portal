"""Configuration.

Everything that differs between a laptop, PythonAnywhere and Railway is read
from the environment here, so the same code runs unchanged on all three.
No credential is ever hard-coded.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    """Where the database and uploads live.

    Kept OUTSIDE the source tree so that redeploying (git pull, file replace)
    can never wipe the data.  Override with DATA_DIR -- on Railway point it at
    the mounted volume, on PythonAnywhere at something like
    /home/<user>/mechexam-data.
    """
    raw = os.environ.get("DATA_DIR")
    path = Path(raw).expanduser() if raw else BASE_DIR / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _database_uri(data_dir: Path) -> str:
    url = os.environ.get("DATABASE_URL") or os.environ.get("SQLALCHEMY_DATABASE_URI")
    if url:
        # SQLAlchemy 2.x dropped the bare "postgres://" scheme that Heroku and
        # Railway still hand out.
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if url.startswith("postgresql://") and "+psycopg" not in url:
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url
    return f"sqlite:///{data_dir / 'mechexam.db'}"


class Config:
    DATA_DIR = _data_dir()
    UPLOAD_DIR = DATA_DIR / "uploads"
    BACKUP_DIR = DATA_DIR / "backups"

    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-only-change-me"
    SQLALCHEMY_DATABASE_URI = _database_uri(DATA_DIR)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 280}

    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "64")) * 1024 * 1024
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_HTTPONLY = True
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12

    # Templates must never be cached in a long-lived worker that we hot-fix.
    TEMPLATES_AUTO_RELOAD = os.environ.get("TEMPLATES_AUTO_RELOAD", "1") == "1"

    # Institution identity -- shown on the dashboard and printed grade sheets.
    SCHOOL_NAME = os.environ.get("SCHOOL_NAME", "Western Philippines University")
    COLLEGE_NAME = os.environ.get("COLLEGE_NAME", "College of Engineering and Technology")
    DEPARTMENT_NAME = os.environ.get("DEPARTMENT_NAME", "Department of Mechanical Engineering")
    CAMPUS_NAME = os.environ.get("CAMPUS_NAME", "Puerto Princesa Campus, Palawan")
    APP_NAME = os.environ.get("APP_NAME", "WPU-CET Examination & Gradebook Portal")

    # Google Drive -- service-account JSON from the environment only.
    GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    GOOGLE_DRIVE_IMPERSONATE = os.environ.get("GOOGLE_DRIVE_IMPERSONATE")

    @staticmethod
    def init_app(app):
        Config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        Config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        (Config.UPLOAD_DIR / "figures").mkdir(parents=True, exist_ok=True)
        (Config.UPLOAD_DIR / "submissions").mkdir(parents=True, exist_ok=True)
