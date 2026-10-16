"""Sign in, sign out, password self-service."""
from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func

from ..extensions import db
from ..models import Setting, User, log_action, utcnow

bp = Blueprint("auth", __name__)


def _safe_next(target: str | None) -> str:
    """Only ever redirect inside this site."""
    if not target:
        return url_for("dashboard.index")
    if target.startswith("//") or "://" in target:
        return url_for("dashboard.index")
    if not target.startswith("/"):
        return url_for("dashboard.index")
    return target


def _find_user(identifier: str) -> User | None:
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    lowered = identifier.lower()
    user = User.query.filter(func.lower(User.username) == lowered).first()
    if user is not None:
        return user
    return User.query.filter(func.lower(User.student_number) == lowered).first()


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "")
        password = request.form.get("password", "")
        user = _find_user(identifier)

        if user is None or not user.check_password(password):
            flash("Those sign-in details were not recognised.", "error")
            log_action(None, "login_failed", "user", None, f"identifier={identifier[:60]}")
            db.session.commit()
            return render_template("auth/login.html", identifier=identifier), 401

        if not user.is_enabled:
            flash("That account has been disabled. Please contact the administrator.", "error")
            return render_template("auth/login.html", identifier=identifier), 403

        login_user(user, remember=bool(request.form.get("remember")))
        user.last_login_at = utcnow()
        log_action(user, "login", "user", user.id)
        db.session.commit()

        if user.must_change_password:
            flash("Please set a new password before continuing.", "info")
            return redirect(url_for("auth.change_password"))

        return redirect(_safe_next(request.args.get("next") or request.form.get("next")))

    return render_template("auth/login.html", identifier="")


@bp.route("/logout")
@login_required
def logout():
    log_action(current_user, "logout", "user", current_user.id)
    db.session.commit()
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    """Students may set their own password; staff too."""
    allow_self = Setting.get_bool("allow_student_self_password", True)
    if current_user.is_student and not allow_self and not current_user.must_change_password:
        flash("Password changes are handled by your instructor.", "info")
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        # A forced first-time change does not require the old password, since
        # it was handed out by the instructor.
        if not current_user.must_change_password and not current_user.check_password(current):
            flash("Your current password was not correct.", "error")
            return render_template("auth/password.html")

        if len(new) < 6:
            flash("Use at least 6 characters.", "error")
            return render_template("auth/password.html")
        if new != confirm:
            flash("The two new passwords did not match.", "error")
            return render_template("auth/password.html")

        current_user.set_password(new)
        current_user.must_change_password = False
        current_user.reset_code = None
        log_action(current_user, "password_changed", "user", current_user.id)
        db.session.commit()
        flash("Your password has been changed.", "success")
        return redirect(url_for("dashboard.index"))

    return render_template("auth/password.html")


@bp.route("/recover", methods=["GET", "POST"])
def recover():
    """Reset with a code issued by the instructor -- no email server needed."""
    if request.method == "POST":
        identifier = request.form.get("identifier", "")
        code = (request.form.get("code", "") or "").strip().upper()
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        user = _find_user(identifier)
        if user is None or not user.reset_code or user.reset_code.upper() != code:
            flash("That reset code is not valid for this account.", "error")
            return render_template("auth/recover.html", identifier=identifier), 400
        if len(new) < 6:
            flash("Use at least 6 characters.", "error")
            return render_template("auth/recover.html", identifier=identifier), 400
        if new != confirm:
            flash("The two new passwords did not match.", "error")
            return render_template("auth/recover.html", identifier=identifier), 400

        user.set_password(new)
        user.reset_code = None
        user.must_change_password = False
        log_action(user, "password_recovered", "user", user.id)
        db.session.commit()
        flash("Your password has been set. You can sign in now.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/recover.html", identifier="")


@bp.route("/theme/<string:mode>")
@login_required
def set_theme(mode: str):
    if mode in ("light", "dark", "system"):
        current_user.theme = mode
        db.session.commit()
    return redirect(request.referrer or url_for("dashboard.index"))
