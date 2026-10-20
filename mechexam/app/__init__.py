"""Application factory."""
from __future__ import annotations

import logging
from datetime import datetime

from flask import Flask, render_template, request
from flask_login import current_user

from . import assets, migrate
from .config import Config
from .extensions import db, login_manager

__version__ = "2.0.0"


def create_app(config_object=Config) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)
    config_object.init_app(app)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db.init_app(app)
    login_manager.init_app(app)
    assets.register(app)

    # Import the models before touching the schema: db.metadata is only
    # populated as the model classes are defined, and a migration run against
    # empty metadata silently does nothing.
    from . import models  # noqa: F401

    # Schema first: reconcile, create, index -- before any ORM query runs.
    migrate.initialise(app)

    from . import seed
    seed.bootstrap(app)

    _register_blueprints(app)
    _register_jinja(app)
    _register_hooks(app)
    _register_errors(app)
    _register_cli(app)

    return app


# --------------------------------------------------------------------------- #

def _register_blueprints(app: Flask) -> None:
    from .blueprints import (
        admin,
        api,
        auth,
        courses,
        dashboard,
        exams,
        gradebook,
        messages,
        reports,
        rubrics,
        student,
        taking,
    )

    app.register_blueprint(auth.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(admin.bp, url_prefix="/admin")
    app.register_blueprint(courses.bp, url_prefix="/subjects")
    app.register_blueprint(exams.bp, url_prefix="/exams")
    app.register_blueprint(taking.bp, url_prefix="/take")
    app.register_blueprint(gradebook.bp, url_prefix="/gradebook")
    app.register_blueprint(rubrics.bp, url_prefix="/rubrics")
    app.register_blueprint(reports.bp, url_prefix="/reports")
    app.register_blueprint(messages.bp, url_prefix="/messages")
    app.register_blueprint(student.bp, url_prefix="/my")
    app.register_blueprint(api.bp, url_prefix="/api")


def _register_jinja(app: Flask) -> None:
    from .models import Message, MessageRead, Setting

    @app.context_processor
    def inject_globals():
        unread = 0
        nav_courses = []
        if current_user.is_authenticated:
            unread = _unread_count(current_user)
            try:
                from .security import visible_courses

                nav_courses = visible_courses().limit(12).all()
            except Exception:
                nav_courses = []
        return {
            "nav_courses": nav_courses,
            "app_name": app.config["APP_NAME"],
            "school_name": app.config["SCHOOL_NAME"],
            "college_name": app.config["COLLEGE_NAME"],
            "department_name": app.config["DEPARTMENT_NAME"],
            "campus_name": app.config["CAMPUS_NAME"],
            "registrar_office": app.config["REGISTRAR_OFFICE"],
            "registrar_campus": app.config["REGISTRAR_CAMPUS"],
            "university_tagline": app.config["UNIVERSITY_TAGLINE"],
            "report_of_grades_form": app.config["REPORT_OF_GRADES_FORM"],
            "app_version": __version__,
            "school_year": Setting.get("school_year"),
            "semester": Setting.get("semester"),
            "unread_messages": unread,
            "now": datetime.now(),
        }

    def _unread_count(user) -> int:
        from .models import Enrollment

        try:
            read_ids = {
                row.message_id
                for row in MessageRead.query.filter_by(user_id=user.id).all()
            }
            query = Message.query.filter(Message.sender_id != user.id)
            if user.is_student:
                course_ids = [
                    e.course_id
                    for e in Enrollment.query.filter_by(
                        student_id=user.id, is_dropped=False
                    ).all()
                ]
                query = query.filter(
                    db.or_(
                        Message.recipient_id == user.id,
                        db.and_(
                            Message.is_announcement.is_(True),
                            Message.course_id.in_(course_ids or [-1]),
                        ),
                    )
                )
            else:
                query = query.filter(Message.recipient_id == user.id)
            return sum(1 for m in query.all() if m.id not in read_ids)
        except Exception:
            return 0

    # ---- filters --------------------------------------------------------- #

    @app.template_filter("pct")
    def _pct(value):
        if value is None:
            return "—"
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "—"

    @app.template_filter("num")
    def _num(value):
        if value is None:
            return "—"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        return f"{number:g}"

    @app.template_filter("dt")
    def _dt(value, fmt="%d %b %Y, %I:%M %p"):
        if not value:
            return "—"
        try:
            return value.strftime(fmt)
        except Exception:
            return str(value)

    @app.template_filter("d")
    def _d(value, fmt="%d %b %Y"):
        if not value:
            return "—"
        try:
            return value.strftime(fmt)
        except Exception:
            return str(value)

    @app.template_filter("mmss")
    def _mmss(seconds):
        try:
            total = max(0, int(seconds))
        except (TypeError, ValueError):
            return "—"
        hours, rest = divmod(total, 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    @app.template_filter("nl2br")
    def _nl2br(value):
        from markupsafe import Markup, escape

        if not value:
            return ""
        return Markup("<br>".join(escape(value).split("\n")))

    from .grading import bump_one_step, transmute
    from .marking import matching_detail

    app.jinja_env.globals["Setting"] = Setting
    app.jinja_env.globals["matching_detail"] = matching_detail
    app.jinja_env.globals["bump_one_step"] = bump_one_step
    app.jinja_env.globals["transmute"] = transmute
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True


def _register_hooks(app: Flask) -> None:
    @app.after_request
    def no_html_cache(response):
        """HTML is never cached.

        Assets carry a content hash and are cached hard; pages are dynamic and
        must not be.  Getting this wrong is what left a host serving a stale
        shell on top of fresh data.
        """
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.before_request
    def touch_session():
        from flask import session

        session.permanent = True

    @app.teardown_appcontext
    def cleanup(exception=None):
        if exception is not None:
            db.session.rollback()
        db.session.remove()


def _register_errors(app: Flask) -> None:
    @app.errorhandler(403)
    def forbidden(error):
        return render_template("errors/error.html", code=403,
                               title="Not allowed",
                               message="You do not have access to that page."), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template("errors/error.html", code=404,
                               title="Page not found",
                               message="That page does not exist, or is not yours to open."), 404

    @app.errorhandler(413)
    def too_large(error):
        limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return render_template("errors/error.html", code=413,
                               title="File too large",
                               message=f"Uploads are limited to {limit} MB."), 413

    @app.errorhandler(500)
    def server_error(error):
        db.session.rollback()
        app.logger.exception("unhandled error at %s", request.path)
        return render_template("errors/error.html", code=500,
                               title="Something went wrong",
                               message="The error has been logged. "
                                       "Administrators can see details on the "
                                       "System check page."), 500


def _register_cli(app: Flask) -> None:
    import click

    @app.cli.command("create-admin")
    @click.option("--username", default="admin")
    @click.option("--password", required=True)
    @click.option("--name", default="Administrator")
    def create_admin(username, password, name):
        """Create or reset a superadmin account."""
        from .models import ROLE_SUPERADMIN, User

        user = User.query.filter_by(username=username).first()
        if user is None:
            user = User(username=username, role=ROLE_SUPERADMIN, name=name)
            db.session.add(user)
        user.role = ROLE_SUPERADMIN
        user.name = name
        user.set_password(password)
        user.must_change_password = False
        db.session.commit()
        click.echo(f"superadmin ready: {username}")

    @app.cli.command("backup")
    def backup_cmd():
        """Write a JSON backup into the data directory."""
        from .backup import export_backup

        path = export_backup()
        click.echo(f"backup written: {path}")
