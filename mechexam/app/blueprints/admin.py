"""Administration: accounts, term settings, diagnostics, backups, audit trail."""
from __future__ import annotations

import json
from datetime import datetime

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user
from sqlalchemy import func, or_

from .. import backup as backup_mod
from .. import drive as drive_mod
from ..extensions import db
from ..grading import ensure_components
from ..models import (
    AuditLog,
    Course,
    Enrollment,
    ROLE_INSTRUCTOR,
    ROLE_STUDENT,
    ROLE_SUPERADMIN,
    ROLES,
    Setting,
    StudentYearStatus,
    User,
    log_action,
)
from ..security import superadmin_required, staff_required

bp = Blueprint("admin", __name__)

SEMESTERS = ("First Semester", "Second Semester", "Summer")


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

@bp.route("/")
@superadmin_required
def index():
    counts = {
        "superadmins": User.query.filter_by(role=ROLE_SUPERADMIN).count(),
        "instructors": User.query.filter_by(role=ROLE_INSTRUCTOR).count(),
        "students": User.query.filter_by(role=ROLE_STUDENT).count(),
        "subjects": Course.query.count(),
        "subjects_this_term": Course.query.filter_by(
            school_year=Setting.get("school_year"), semester=Setting.get("semester")
        ).count(),
    }
    recent = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(12).all()
    return render_template("admin/index.html", counts=counts, recent=recent)


# --------------------------------------------------------------------------- #
# Term settings
# --------------------------------------------------------------------------- #

@bp.route("/term", methods=["GET", "POST"])
@superadmin_required
def term():
    if request.method == "POST":
        year = (request.form.get("school_year") or "").strip()
        semester = (request.form.get("semester") or "").strip()

        if not year or semester not in SEMESTERS:
            flash("Give a school year and choose a semester.", "error")
            return redirect(url_for("admin.term"))

        Setting.put("school_year", year)
        Setting.put("semester", semester)
        for key in ("removal_low", "removal_high", "passing_percent",
                    "lecture_share", "laboratory_share", "exam_autosave_seconds"):
            if request.form.get(key):
                Setting.put(key, request.form.get(key))
        Setting.put("default_averaging", request.form.get("default_averaging", "total_points"))
        Setting.put("allow_student_self_password",
                    "1" if request.form.get("allow_student_self_password") else "0")
        Setting.put("grades_released_default",
                    "1" if request.form.get("grades_released_default") else "0")

        log_action(current_user, "term_changed", "setting", None, f"{semester} {year}")
        db.session.commit()
        flash(f"The portal is now set to {semester}, {year}.", "success")
        return redirect(url_for("admin.term"))

    return render_template(
        "admin/term.html",
        semesters=SEMESTERS,
        suggestion=_next_term(Setting.get("school_year"), Setting.get("semester")),
    )


def _next_term(year: str, semester: str) -> dict:
    """First -> Second -> next year's First.  Summer is set by hand."""
    if semester == "First Semester":
        return {"school_year": year, "semester": "Second Semester"}
    if semester == "Second Semester":
        try:
            start, end = year.split("-")
            nxt = f"{int(start) + 1}-{int(end) + 1}"
        except Exception:
            nxt = year
        return {"school_year": nxt, "semester": "First Semester"}
    return {"school_year": year, "semester": "First Semester"}


@bp.route("/term/rollover", methods=["POST"])
@superadmin_required
def rollover():
    """End the semester and move the portal to the next one.

    Subjects and students are *not* deleted -- they stay attached to the term
    they belong to, and the new term starts with a clean subject list that the
    instructor fills by copying subjects forward.
    """
    year = Setting.get("school_year")
    semester = Setting.get("semester")
    target = _next_term(year, semester)

    requested_year = (request.form.get("school_year") or "").strip()
    requested_semester = (request.form.get("semester") or "").strip()
    if requested_year:
        target["school_year"] = requested_year
    if requested_semester in SEMESTERS:
        target["semester"] = requested_semester

    if request.form.get("carry_subjects"):
        carried = _carry_subjects(year, semester, target)
        flash(f"{carried} subject(s) carried into the new term.", "info")

    Setting.put("school_year", target["school_year"])
    Setting.put("semester", target["semester"])
    log_action(
        current_user, "semester_ended", "setting", None,
        f"{semester} {year} -> {target['semester']} {target['school_year']}",
    )
    db.session.commit()
    flash(
        f"{semester}, {year} has been closed. The portal is now on "
        f"{target['semester']}, {target['school_year']}.",
        "success",
    )
    return redirect(url_for("admin.term"))


def _carry_subjects(year: str, semester: str, target: dict) -> int:
    """Copy subject shells (code, title, components, sections) forward."""
    from ..models import GradeComponent, Section

    source = Course.query.filter_by(school_year=year, semester=semester).all()
    made = 0
    for course in source:
        exists = Course.query.filter_by(
            code=course.code,
            school_year=target["school_year"],
            semester=target["semester"],
        ).first()
        if exists is not None:
            continue

        clone = Course(
            code=course.code,
            title=course.title,
            description=course.description,
            units=course.units,
            lec_units=course.lec_units,
            lab_units=course.lab_units,
            has_laboratory=course.has_laboratory,
            owner_id=course.owner_id,
            school_year=target["school_year"],
            semester=target["semester"],
            averaging_method=course.averaging_method,
            lecture_share=course.lecture_share,
            laboratory_share=course.laboratory_share,
            removal_low=course.removal_low,
            removal_high=course.removal_high,
        )
        db.session.add(clone)
        db.session.flush()

        for component in course.components:
            db.session.add(
                GradeComponent(
                    course_id=clone.id,
                    name=component.name,
                    scope=component.scope,
                    weight_percent=component.weight_percent,
                    kind=component.kind,
                    order_index=component.order_index,
                )
            )
        for section in course.sections:
            db.session.add(
                Section(
                    course_id=clone.id,
                    name=section.name,
                    schedule=section.schedule,
                    room=section.room,
                    capacity=section.capacity,
                )
            )
        made += 1
    db.session.commit()
    return made


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #

@bp.route("/users")
@superadmin_required
def users():
    role = request.args.get("role", "")
    search = (request.args.get("q") or "").strip()

    query = User.query
    if role in ROLES:
        query = query.filter_by(role=role)
    if search:
        pattern = f"%{search.lower()}%"
        query = query.filter(
            or_(
                func.lower(User.name).like(pattern),
                func.lower(User.username).like(pattern),
                func.lower(User.student_number).like(pattern),
            )
        )

    page = request.args.get("page", 1, type=int)
    listing = query.order_by(User.role, User.name).paginate(
        page=page, per_page=50, error_out=False
    )
    return render_template(
        "admin/users.html", listing=listing, role=role, search=search, roles=ROLES
    )


@bp.route("/users/new", methods=["GET", "POST"])
@superadmin_required
def user_new():
    if request.method == "POST":
        return _save_user(None)
    return render_template("admin/user_form.html", user=None, roles=ROLES)


@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
@superadmin_required
def user_edit(user_id: int):
    user = User.query.get_or_404(user_id)
    if request.method == "POST":
        return _save_user(user)
    statuses = (
        StudentYearStatus.query.filter_by(student_id=user.id)
        .order_by(StudentYearStatus.school_year.desc())
        .all()
    )
    enrolments = (
        Enrollment.query.filter_by(student_id=user.id).all() if user.is_student else []
    )
    return render_template(
        "admin/user_form.html",
        user=user,
        roles=ROLES,
        statuses=statuses,
        enrolments=enrolments,
        year_status_choices=StudentYearStatus.STATUSES,
        status_labels=StudentYearStatus.LABELS,
    )


def _save_user(user: User | None):
    form = request.form
    username = (form.get("username") or "").strip()
    role = form.get("role") or ROLE_STUDENT
    name = (form.get("name") or "").strip()

    if not username or not name:
        flash("A username and a full name are required.", "error")
        return redirect(request.url)
    if role not in ROLES:
        flash("Choose a valid role.", "error")
        return redirect(request.url)

    clash = User.query.filter(
        func.lower(User.username) == username.lower(),
        User.id != (user.id if user else -1),
    ).first()
    if clash is not None:
        flash(f"The username {username!r} is already taken.", "error")
        return redirect(request.url)

    student_number = (form.get("student_number") or "").strip() or None
    if student_number:
        clash = User.query.filter(
            func.lower(User.student_number) == student_number.lower(),
            User.id != (user.id if user else -1),
        ).first()
        if clash is not None:
            flash(f"Student number {student_number!r} is already on another account.", "error")
            return redirect(request.url)

    created = user is None
    if created:
        user = User(username=username, role=role, name=name)
        db.session.add(user)

    user.username = username
    user.role = role
    user.name = name
    user.student_number = student_number
    user.email = (form.get("email") or "").strip()
    user.program = (form.get("program") or "").strip()
    user.year_level = (form.get("year_level") or "").strip()
    user.contact = (form.get("contact") or "").strip()
    user.rank = (form.get("rank") or "").strip()
    user.is_enabled = bool(form.get("is_enabled")) or created

    password = form.get("password") or ""
    if password:
        if len(password) < 6:
            flash("Passwords need at least 6 characters.", "error")
            return redirect(request.url)
        user.set_password(password)
        user.must_change_password = bool(form.get("must_change_password"))
    elif created:
        # Sensible default: the student number, or the username.
        user.set_password(student_number or username)
        user.must_change_password = True

    log_action(
        current_user,
        "user_created" if created else "user_updated",
        "user",
        user.id,
        f"{role} {username}",
    )
    db.session.commit()
    flash(f"{'Created' if created else 'Updated'} the account for {name}.", "success")
    return redirect(url_for("admin.user_edit", user_id=user.id))


@bp.route("/users/<int:user_id>/reset-code", methods=["POST"])
@superadmin_required
def user_reset_code(user_id: int):
    user = User.query.get_or_404(user_id)
    code = user.issue_reset_code()
    log_action(current_user, "reset_code_issued", "user", user.id)
    db.session.commit()
    flash(
        f"Reset code for {user.display_name}: {code} — give this to them, "
        f"then they can set a new password from the sign-in page.",
        "success",
    )
    return redirect(url_for("admin.user_edit", user_id=user.id))


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@superadmin_required
def user_toggle(user_id: int):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot disable your own account.", "error")
        return redirect(url_for("admin.users"))
    user.is_enabled = not user.is_enabled
    log_action(current_user, "user_enabled" if user.is_enabled else "user_disabled",
               "user", user.id)
    db.session.commit()
    flash(
        f"{user.display_name} has been {'enabled' if user.is_enabled else 'disabled'}.",
        "success",
    )
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/year-status", methods=["POST"])
@staff_required
def year_status(user_id: int):
    """Shifted / transferred / stopped for a whole school year."""
    user = User.query.get_or_404(user_id)
    if not user.is_student:
        abort(400)

    year = (request.form.get("school_year") or Setting.get("school_year")).strip()
    status = request.form.get("status") or "active"
    if status not in StudentYearStatus.STATUSES:
        flash("Choose a valid standing.", "error")
        return redirect(request.referrer or url_for("admin.user_edit", user_id=user.id))

    row = StudentYearStatus.query.filter_by(student_id=user.id, school_year=year).first()
    if row is None:
        row = StudentYearStatus(student_id=user.id, school_year=year)
        db.session.add(row)
    row.status = status
    row.note = (request.form.get("note") or "").strip()

    log_action(current_user, "year_status", "user", user.id, f"{year}: {status}")
    db.session.commit()

    label = StudentYearStatus.LABELS.get(status, status)
    if status == "active":
        flash(f"{user.display_name} is active again for {year}.", "success")
    else:
        flash(
            f"{user.display_name} is marked “{label}” for {year} and will not "
            f"appear in any class list for that year.",
            "success",
        )
    return redirect(request.referrer or url_for("admin.user_edit", user_id=user.id))


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@superadmin_required
def user_delete(user_id: int):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin.users"))

    owned = Course.query.filter_by(owner_id=user.id).count()
    if owned:
        flash(
            f"{user.display_name} is assigned to {owned} subject(s). "
            f"Reassign those first, or disable the account instead.",
            "error",
        )
        return redirect(url_for("admin.user_edit", user_id=user.id))

    name = user.display_name
    Enrollment.query.filter_by(student_id=user.id).delete(synchronize_session=False)
    StudentYearStatus.query.filter_by(student_id=user.id).delete(synchronize_session=False)
    db.session.delete(user)
    log_action(current_user, "user_deleted", "user", user_id, name)
    db.session.commit()
    flash(f"Deleted the account for {name}.", "success")
    return redirect(url_for("admin.users"))


# --------------------------------------------------------------------------- #
# Subjects (creation is superadmin-only, by design)
# --------------------------------------------------------------------------- #

@bp.route("/subjects", methods=["GET", "POST"])
@superadmin_required
def subjects():
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        title = (request.form.get("title") or "").strip()
        if not code or not title:
            flash("A subject code and title are required.", "error")
            return redirect(url_for("admin.subjects"))

        year = (request.form.get("school_year") or Setting.get("school_year")).strip()
        semester = (request.form.get("semester") or Setting.get("semester")).strip()

        if Course.query.filter_by(code=code, school_year=year, semester=semester).first():
            flash(f"{code} already exists for {semester}, {year}.", "error")
            return redirect(url_for("admin.subjects"))

        owner_id = request.form.get("owner_id", type=int)
        course = Course(
            code=code,
            title=title,
            description=(request.form.get("description") or "").strip(),
            units=request.form.get("units", type=float) or 3.0,
            lec_units=request.form.get("lec_units", type=float) or 3.0,
            lab_units=request.form.get("lab_units", type=float) or 0.0,
            has_laboratory=bool(request.form.get("has_laboratory")),
            owner_id=owner_id or None,
            school_year=year,
            semester=semester,
            averaging_method=Setting.get("default_averaging", "total_points"),
            lecture_share=Setting.get_float("lecture_share", 67.0),
            laboratory_share=Setting.get_float("laboratory_share", 33.0),
            removal_low=Setting.get_float("removal_low", 55.0),
            removal_high=Setting.get_float("removal_high", 59.99),
            grades_released=Setting.get_bool("grades_released_default", False),
        )
        db.session.add(course)
        db.session.commit()
        ensure_components(course)
        log_action(current_user, "subject_created", "course", course.id, course.label)
        db.session.commit()
        flash(f"Created {course.label}.", "success")
        return redirect(url_for("courses.detail", course_id=course.id))

    year = request.args.get("school_year") or Setting.get("school_year")
    semester = request.args.get("semester") or Setting.get("semester")
    listing = (
        Course.query.filter_by(school_year=year, semester=semester)
        .order_by(Course.code)
        .all()
    )
    years = [
        row[0]
        for row in db.session.query(Course.school_year).distinct().order_by(Course.school_year)
    ]
    if Setting.get("school_year") not in years:
        years.append(Setting.get("school_year"))

    return render_template(
        "admin/subjects.html",
        listing=listing,
        instructors=User.query.filter_by(role=ROLE_INSTRUCTOR, is_enabled=True)
        .order_by(User.name)
        .all(),
        semesters=SEMESTERS,
        years=sorted(set(years)),
        filter_year=year,
        filter_semester=semester,
    )


@bp.route("/subjects/<int:course_id>/assign", methods=["POST"])
@superadmin_required
def subject_assign(course_id: int):
    course = Course.query.get_or_404(course_id)
    owner_id = request.form.get("owner_id", type=int)
    course.owner_id = owner_id or None
    log_action(current_user, "subject_assigned", "course", course.id,
               f"owner={owner_id}")
    db.session.commit()
    flash("Instructor assignment saved.", "success")
    return redirect(request.referrer or url_for("admin.subjects"))


@bp.route("/subjects/<int:course_id>/delete", methods=["POST"])
@superadmin_required
def subject_delete(course_id: int):
    """Only the superadmin may delete a subject, and only an empty one."""
    course = Course.query.get_or_404(course_id)
    from ..models import Exam

    enrolled = Enrollment.query.filter_by(course_id=course.id).count()
    exams = Exam.query.filter_by(course_id=course.id).count()
    if enrolled or exams:
        flash(
            f"{course.code} still has {enrolled} student(s) and {exams} exam(s). "
            f"Empty it first — this guard exists so a term's records cannot be "
            f"deleted by accident.",
            "error",
        )
        return redirect(url_for("courses.detail", course_id=course.id))

    label = course.label
    db.session.delete(course)
    log_action(current_user, "subject_deleted", "course", course_id, label)
    db.session.commit()
    flash(f"Deleted {label}.", "success")
    return redirect(url_for("admin.subjects"))


# --------------------------------------------------------------------------- #
# Diagnostics, backups, audit
# --------------------------------------------------------------------------- #

@bp.route("/system")
@superadmin_required
def system():
    """Everything needed to diagnose a deployment without a developer."""
    from .. import __version__
    from ..assets import digest

    engine_url = str(db.engine.url)
    if "@" in engine_url:                    # never show a password
        scheme, rest = engine_url.split("://", 1)
        engine_url = f"{scheme}://…@{rest.split('@', 1)[1]}"

    table_counts = {}
    for table in db.metadata.sorted_tables:
        try:
            table_counts[table.name] = db.session.execute(
                db.select(func.count()).select_from(table)
            ).scalar()
        except Exception:
            table_counts[table.name] = "—"

    info = {
        "version": __version__,
        "dialect": db.engine.name,
        "database": engine_url,
        "data_dir": str(current_app.config["DATA_DIR"]),
        "upload_dir": str(current_app.config["UPLOAD_DIR"]),
        "max_upload_mb": current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024),
        "schema": current_app.config.get("SCHEMA_REPORT", {}),
        "css_digest": digest("css/app.css"),
        "js_digest": digest("js/app.js"),
        "katex_present": (
            (current_app.root_path + "/static/vendor/katex/katex.min.css")
            and __import__("os").path.exists(
                current_app.root_path + "/static/vendor/katex/katex.min.css"
            )
        ),
        "drive": drive_mod.explain(),
        "term": f"{Setting.get('semester')}, {Setting.get('school_year')}",
        "server_time": datetime.now().strftime("%d %b %Y, %I:%M:%S %p"),
    }
    return render_template(
        "admin/system.html",
        info=info,
        table_counts=table_counts,
        backups=backup_mod.list_backups(),
    )


@bp.route("/system/backup", methods=["POST"])
@superadmin_required
def make_backup():
    try:
        path = backup_mod.export_backup()
    except Exception as exc:
        flash(f"The backup could not be written: {exc}", "error")
        return redirect(url_for("admin.system"))

    log_action(current_user, "backup_created", "system", None, path.name)
    db.session.commit()
    return send_file(path, as_attachment=True, download_name=path.name)


@bp.route("/system/backup/<path:name>")
@superadmin_required
def download_backup(name: str):
    directory = current_app.config["BACKUP_DIR"]
    target = (directory / name).resolve()
    if directory.resolve() not in target.parents or not target.is_file():
        abort(404)
    return send_file(target, as_attachment=True, download_name=target.name)


@bp.route("/system/restore", methods=["POST"])
@superadmin_required
def restore():
    upload = request.files.get("backup")
    if upload is None or not upload.filename:
        flash("Choose a backup file first.", "error")
        return redirect(url_for("admin.system"))

    mode_wipe = request.form.get("mode") != "merge"
    if mode_wipe and request.form.get("confirm") != "REPLACE":
        flash(
            "A full restore replaces everything. Type REPLACE in the "
            "confirmation box to go ahead.",
            "error",
        )
        return redirect(url_for("admin.system"))

    try:
        payload = json.loads(upload.read().decode("utf-8"))
    except Exception as exc:
        flash(f"That file could not be read as a portal backup: {exc}", "error")
        return redirect(url_for("admin.system"))

    # Keep a safety copy of the present state before overwriting it.
    try:
        safety = backup_mod.export_backup()
    except Exception:
        safety = None

    try:
        report = backup_mod.restore_snapshot(payload, wipe=mode_wipe)
    except Exception as exc:
        flash(f"The restore failed: {exc}", "error")
        return redirect(url_for("admin.system"))

    total = sum(report["restored"].values())
    message = f"Restored {total} row(s) across {len(report['restored'])} table(s)."
    if safety is not None:
        message += f" The previous state was saved as {safety.name}."
    flash(message, "success")
    if report["errors"]:
        flash(
            "Some rows were skipped: " + "; ".join(report["errors"][:5]),
            "warning",
        )
    return redirect(url_for("admin.system"))


@bp.route("/audit")
@superadmin_required
def audit():
    page = request.args.get("page", 1, type=int)
    action = (request.args.get("action") or "").strip()
    query = AuditLog.query
    if action:
        query = query.filter(AuditLog.action == action)
    listing = query.order_by(AuditLog.created_at.desc()).paginate(
        page=page, per_page=80, error_out=False
    )
    actions = [
        row[0]
        for row in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action)
    ]
    return render_template(
        "admin/audit.html", listing=listing, actions=actions, action=action
    )
