"""Subjects: the page an instructor actually works from.

Sections, the class roster, enrolment, drops, attendance, and the ability to
add a performance task without detouring through the gradebook -- all on the
subject page, which is what the instructor asked for.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from sqlalchemy import func, or_

from ..extensions import db
from ..grading import ensure_components
from ..models import (
    AttendanceRecord,
    AttendanceSession,
    Course,
    Enrollment,
    Exam,
    GradeComponent,
    GradeItem,
    ROLE_STUDENT,
    Rubric,
    SCOPE_LABORATORY,
    SCOPE_LECTURE,
    Section,
    Setting,
    StudentYearStatus,
    User,
    log_action,
    utcnow,
)
from ..security import (
    require_course,
    staff_required,
    visible_courses,
)

bp = Blueprint("courses", __name__)


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #

@bp.route("/")
@staff_required
def index():
    courses = visible_courses().all()
    rows = []
    for course in courses:
        rows.append(
            {
                "course": course,
                "students": Enrollment.query.filter_by(
                    course_id=course.id, is_dropped=False
                ).count(),
                "sections": len(course.sections),
                "exams": Exam.query.filter_by(course_id=course.id).count(),
            }
        )
    return render_template("courses/index.html", rows=rows)


# --------------------------------------------------------------------------- #
# Subject page
# --------------------------------------------------------------------------- #

@bp.route("/<int:course_id>")
@staff_required
def detail(course_id: int):
    course = require_course(course_id)
    section_id = request.args.get("section", type=int)

    sections = list(course.sections)
    students = course.roster(section_id)

    # One query for every student's section, rather than one per table row.
    section_names = {s.id: s.name for s in sections}
    student_sections = {
        enr.student_id: section_names.get(enr.section_id, "")
        for enr in Enrollment.query.filter_by(course_id=course.id).all()
    }

    exams = (
        Exam.query.filter_by(course_id=course.id)
        .order_by(Exam.created_at.desc())
        .all()
    )

    lecture = [c for c in course.components if c.scope == SCOPE_LECTURE]
    laboratory = [c for c in course.components if c.scope == SCOPE_LABORATORY]
    lecture.sort(key=lambda c: (c.order_index or 0, c.id))
    laboratory.sort(key=lambda c: (c.order_index or 0, c.id))

    rubrics = (
        Rubric.query.filter(
            or_(Rubric.course_id == course.id, Rubric.is_template.is_(True))
        )
        .order_by(Rubric.is_template, Rubric.name)
        .all()
    )

    dropped = (
        Enrollment.query.filter_by(course_id=course.id, is_dropped=True)
        .join(User, Enrollment.student_id == User.id)
        .order_by(User.name)
        .all()
    )

    return render_template(
        "courses/detail.html",
        course=course,
        sections=sections,
        section_id=section_id,
        students=students,
        student_sections=student_sections,
        dropped=dropped,
        exams=exams,
        lecture_components=lecture,
        laboratory_components=laboratory,
        rubrics=rubrics,
        item_types=GradeItem.TYPES,
    )


@bp.route("/<int:course_id>/settings", methods=["POST"])
@staff_required
def settings(course_id: int):
    course = require_course(course_id)
    form = request.form

    course.title = (form.get("title") or course.title).strip()
    course.description = (form.get("description") or "").strip()
    course.units = form.get("units", type=float) or course.units
    course.lec_units = form.get("lec_units", type=float) or course.lec_units
    course.lab_units = form.get("lab_units", type=float) or 0.0

    had_lab = course.has_laboratory
    course.has_laboratory = bool(form.get("has_laboratory"))

    course.averaging_method = (
        form.get("averaging_method") or "total_points"
    )
    # The lecture and laboratory shares belong to the subject, not the portal:
    # ES 105A and a laboratory-heavy subject do not split the same way, and
    # one figure for the whole college would be wrong for most of it. The
    # portal-wide numbers are only the starting point for a new subject.
    lecture_share = form.get("lecture_share", type=float)
    laboratory_share = form.get("laboratory_share", type=float)
    if lecture_share is not None:
        course.lecture_share = max(0.0, min(100.0, lecture_share))
    if laboratory_share is not None:
        course.laboratory_share = max(0.0, min(100.0, laboratory_share))

    if course.has_laboratory:
        total_share = (course.lecture_share or 0.0) + (course.laboratory_share or 0.0)
        if total_share <= 0:
            flash(
                "The lecture and laboratory shares cannot both be zero — "
                "the final grade would have nothing to come from. Reset to "
                "67 and 33.",
                "error",
            )
            course.lecture_share, course.laboratory_share = 67.0, 33.0

    course.removal_low = form.get("removal_low", type=float) or 55.0
    course.removal_high = form.get("removal_high", type=float) or 59.99

    if course.removal_low >= course.removal_high:
        flash("The removal band's lower figure must be below the upper one.", "error")
        return redirect(url_for("courses.detail", course_id=course.id))

    db.session.commit()

    if course.has_laboratory and not had_lab:
        ensure_components(course)
        flash("Laboratory components have been added with the CET default weights.", "info")

    log_action(current_user, "subject_settings", "course", course.id)
    db.session.commit()
    flash("Subject settings saved.", "success")
    return redirect(url_for("courses.detail", course_id=course.id))


@bp.route("/<int:course_id>/release-grades", methods=["POST"])
@staff_required
def release_grades(course_id: int):
    """Grades stay hidden from students until the instructor publishes them."""
    course = require_course(course_id)
    course.grades_released = not course.grades_released
    log_action(
        current_user,
        "grades_released" if course.grades_released else "grades_hidden",
        "course",
        course.id,
    )
    db.session.commit()
    flash(
        "Students can now see their standing in this subject."
        if course.grades_released
        else "Grades are hidden from students again.",
        "success",
    )
    return redirect(request.referrer or url_for("courses.detail", course_id=course.id))


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #

@bp.route("/<int:course_id>/sections", methods=["POST"])
@staff_required
def section_add(course_id: int):
    course = require_course(course_id)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Give the section a name.", "error")
        return redirect(url_for("courses.detail", course_id=course.id))

    if Section.query.filter(
        Section.course_id == course.id, func.lower(Section.name) == name.lower()
    ).first():
        flash(f"Section {name} already exists in this subject.", "error")
        return redirect(url_for("courses.detail", course_id=course.id))

    db.session.add(
        Section(
            course_id=course.id,
            name=name,
            schedule=(request.form.get("schedule") or "").strip(),
            room=(request.form.get("room") or "").strip(),
            capacity=request.form.get("capacity", type=int) or 0,
            program=(request.form.get("program") or "").strip(),
        )
    )
    log_action(current_user, "section_added", "course", course.id, name)
    db.session.commit()
    flash(f"Section {name} added.", "success")
    return redirect(url_for("courses.detail", course_id=course.id))


@bp.route("/<int:course_id>/sections/<int:section_id>", methods=["POST"])
@staff_required
def section_edit(course_id: int, section_id: int):
    course = require_course(course_id)
    section = Section.query.filter_by(id=section_id, course_id=course.id).first_or_404()

    if request.form.get("action") == "delete":
        count = Enrollment.query.filter_by(
            course_id=course.id, section_id=section.id
        ).count()
        if count:
            flash(
                f"Section {section.name} still has {count} student(s). "
                f"Move them to another section first.",
                "error",
            )
        else:
            db.session.delete(section)
            log_action(current_user, "section_deleted", "course", course.id, section.name)
            db.session.commit()
            flash(f"Section {section.name} removed.", "success")
        return redirect(url_for("courses.detail", course_id=course.id))

    section.name = (request.form.get("name") or section.name).strip()
    section.schedule = (request.form.get("schedule") or "").strip()
    section.room = (request.form.get("room") or "").strip()
    section.capacity = request.form.get("capacity", type=int) or 0
    section.program = (request.form.get("program") or "").strip()
    db.session.commit()
    flash("Section updated.", "success")
    return redirect(url_for("courses.detail", course_id=course.id))


# --------------------------------------------------------------------------- #
# Enrolment
# --------------------------------------------------------------------------- #

@bp.route("/<int:course_id>/enrol", methods=["GET", "POST"])
@staff_required
def enrol(course_id: int):
    """Enrol students into a specific section.

    The previous build's bug was enrolling *every* listed student regardless
    of the chosen section, because the section filter was presentational only.
    Here the target section is required, the candidate list is filtered on the
    server, and only ticked ids are acted on.
    """
    course = require_course(course_id)
    sections = list(course.sections)

    if request.method == "POST":
        section_id = request.form.get("section_id", type=int)
        if sections and not section_id:
            flash("Choose the section these students belong to.", "error")
            return redirect(url_for("courses.enrol", course_id=course.id))
        if section_id and not any(s.id == section_id for s in sections):
            abort(400)

        chosen = request.form.getlist("student_ids", type=int)
        if not chosen:
            flash("No students were ticked.", "warning")
            return redirect(
                url_for("courses.enrol", course_id=course.id, section=section_id)
            )

        added = moved = skipped = 0
        for student_id in chosen:
            student = User.query.filter_by(id=student_id, role=ROLE_STUDENT).first()
            if student is None:
                continue

            existing = Enrollment.query.filter_by(
                course_id=course.id, student_id=student_id
            ).first()
            if existing is None:
                db.session.add(
                    Enrollment(
                        course_id=course.id,
                        student_id=student_id,
                        section_id=section_id,
                    )
                )
                added += 1
            elif existing.is_dropped:
                existing.is_dropped = False
                existing.dropped_at = None
                existing.drop_note = ""
                existing.section_id = section_id or existing.section_id
                added += 1
            elif section_id and existing.section_id != section_id:
                existing.section_id = section_id
                moved += 1
            else:
                skipped += 1

        log_action(
            current_user, "enrolled", "course", course.id,
            f"section={section_id} added={added} moved={moved}",
        )
        db.session.commit()

        parts = []
        if added:
            parts.append(f"{added} enrolled")
        if moved:
            parts.append(f"{moved} moved to this section")
        if skipped:
            parts.append(f"{skipped} already enrolled")
        flash("; ".join(parts) + "." if parts else "Nothing to change.", "success")
        return redirect(url_for("courses.detail", course_id=course.id, section=section_id))

    # ---- candidate list ---------------------------------------------------- #
    search = (request.args.get("q") or "").strip()
    program = (request.args.get("program") or "").strip()
    year_level = (request.args.get("year_level") or "").strip()

    enrolled_ids = {
        row.student_id
        for row in Enrollment.query.filter_by(course_id=course.id, is_dropped=False).all()
    }
    blocked_ids = {
        row.student_id
        for row in StudentYearStatus.query.filter(
            StudentYearStatus.school_year == course.school_year,
            StudentYearStatus.status != "active",
        ).all()
    }

    query = User.query.filter_by(role=ROLE_STUDENT, is_enabled=True)
    if search:
        pattern = f"%{search.lower()}%"
        query = query.filter(
            or_(
                func.lower(User.name).like(pattern),
                func.lower(User.student_number).like(pattern),
            )
        )
    if program:
        query = query.filter(User.program == program)
    if year_level:
        query = query.filter(User.year_level == year_level)

    candidates = [
        student
        for student in query.order_by(User.name).limit(500).all()
        if student.id not in enrolled_ids and student.id not in blocked_ids
    ]

    programs = [
        row[0]
        for row in db.session.query(User.program)
        .filter(User.role == ROLE_STUDENT, User.program != "")
        .distinct()
        .order_by(User.program)
    ]
    year_levels = [
        row[0]
        for row in db.session.query(User.year_level)
        .filter(User.role == ROLE_STUDENT, User.year_level != "")
        .distinct()
        .order_by(User.year_level)
    ]

    return render_template(
        "courses/enrol.html",
        course=course,
        sections=sections,
        candidates=candidates,
        search=search,
        program=program,
        year_level=year_level,
        programs=programs,
        year_levels=year_levels,
        preselect_section=request.args.get("section", type=int),
    )


@bp.route("/<int:course_id>/enrolment/<int:enrollment_id>", methods=["POST"])
@staff_required
def enrolment_change(course_id: int, enrollment_id: int):
    course = require_course(course_id)
    enrolment = Enrollment.query.filter_by(
        id=enrollment_id, course_id=course.id
    ).first_or_404()
    action = request.form.get("action")

    if action == "drop":
        enrolment.is_dropped = True
        enrolment.dropped_at = utcnow()
        enrolment.drop_note = (request.form.get("note") or "").strip()
        log_action(current_user, "student_dropped", "enrollment", enrolment.id)
        db.session.commit()
        flash(
            f"{enrolment.student.display_name} is marked dropped in this subject. "
            f"Their recorded scores are kept, and they can be reinstated.",
            "success",
        )
    elif action == "reinstate":
        enrolment.is_dropped = False
        enrolment.dropped_at = None
        enrolment.drop_note = ""
        log_action(current_user, "student_reinstated", "enrollment", enrolment.id)
        db.session.commit()
        flash(f"{enrolment.student.display_name} is back in the class list.", "success")
    elif action == "move":
        section_id = request.form.get("section_id", type=int)
        if section_id and not any(s.id == section_id for s in course.sections):
            abort(400)
        enrolment.section_id = section_id or None
        db.session.commit()
        flash(f"{enrolment.student.display_name} moved section.", "success")
    elif action == "remove":
        name = enrolment.student.display_name
        db.session.delete(enrolment)
        log_action(current_user, "enrolment_removed", "course", course.id, name)
        db.session.commit()
        flash(f"{name} removed from this subject entirely.", "success")

    return redirect(request.referrer or url_for("courses.detail", course_id=course.id))


# --------------------------------------------------------------------------- #
# Student lists
# --------------------------------------------------------------------------- #

@bp.route("/students")
@staff_required
def term_students():
    """The general roll for the term: everyone, with their standing."""
    year = request.args.get("school_year") or Setting.get("school_year")
    search = (request.args.get("q") or "").strip()
    status_filter = (request.args.get("status") or "").strip()

    query = User.query.filter_by(role=ROLE_STUDENT)
    if search:
        pattern = f"%{search.lower()}%"
        query = query.filter(
            or_(
                func.lower(User.name).like(pattern),
                func.lower(User.student_number).like(pattern),
            )
        )

    students = query.order_by(User.name).all()
    statuses = {
        row.student_id: row
        for row in StudentYearStatus.query.filter_by(school_year=year).all()
    }

    rows = []
    for student in students:
        status = statuses.get(student.id)
        code = status.status if status is not None else "active"
        if status_filter and code != status_filter:
            continue
        subject_count = (
            Enrollment.query.join(Course, Enrollment.course_id == Course.id)
            .filter(
                Enrollment.student_id == student.id,
                Enrollment.is_dropped.is_(False),
                Course.school_year == year,
            )
            .count()
        )
        rows.append({"student": student, "status": code,
                     "note": status.note if status else "",
                     "subjects": subject_count})

    years = [
        row[0]
        for row in db.session.query(StudentYearStatus.school_year).distinct()
    ] + [Setting.get("school_year")]

    return render_template(
        "courses/term_students.html",
        rows=rows,
        year=year,
        years=sorted(set(y for y in years if y)),
        search=search,
        status_filter=status_filter,
        status_choices=StudentYearStatus.STATUSES,
        status_labels=StudentYearStatus.LABELS,
    )


@bp.route("/<int:course_id>/class-list")
@staff_required
def class_list(course_id: int):
    """Per-section class list, the one that gets printed and posted."""
    course = require_course(course_id, manage=False)
    section_id = request.args.get("section", type=int)
    students = course.roster(section_id)

    enrolments = {
        e.student_id: e for e in course.active_enrollments(section_id).all()
    }
    return render_template(
        "courses/class_list.html",
        course=course,
        sections=list(course.sections),
        section_id=section_id,
        students=students,
        enrolments=enrolments,
    )


def _find_or_create_student(
    number: str, name: str, program: str = "", year_level: str = "",
    password: str = "",
) -> tuple[User, bool]:
    """Look a student up by student number, creating the account if new.

    Returns ``(student, created)``. The student number is the identity here,
    not the name: two students may share a name, and a name may be typed
    differently in two class lists, so matching on it would merge records that
    belong to different people.
    """
    student = User.query.filter(
        func.lower(User.student_number) == number.lower()
    ).first()
    if student is not None:
        if program:
            student.program = program
        if year_level:
            student.year_level = year_level
        return student, False

    username = number.replace(" ", "").lower()
    if User.query.filter(func.lower(User.username) == username).first():
        username = f"{username}-{int(datetime.now().timestamp()) % 10000}"
    student = User(
        username=username,
        student_number=number,
        name=name,
        role=ROLE_STUDENT,
        program=program,
        year_level=year_level,
    )
    student.set_password(password or number)
    student.must_change_password = True
    db.session.add(student)
    db.session.flush()
    return student, True


def _enrol_student(course: Course, student: User, section_id) -> str:
    """Enrol, or bring back a dropped enrolment. Returns what happened."""
    existing = Enrollment.query.filter_by(
        course_id=course.id, student_id=student.id
    ).first()
    if existing is None:
        db.session.add(
            Enrollment(
                course_id=course.id, student_id=student.id, section_id=section_id
            )
        )
        return "enrolled"
    if existing.is_dropped:
        existing.is_dropped = False
        existing.dropped_at = None
        existing.section_id = section_id or existing.section_id
        return "readmitted"
    if section_id and existing.section_id != section_id:
        existing.section_id = section_id
        return "moved"
    return "already"


@bp.route("/<int:course_id>/add-student", methods=["POST"])
@staff_required
def add_student(course_id: int):
    """Add one student to this subject, creating the account if they are new.

    The common case on the subject page is a single late enrolee, and sending
    an instructor to the administration pages to create an account and then
    back here to enrol them is two detours for one student.
    """
    course = require_course(course_id)
    section_id = request.form.get("section_id", type=int)
    number = (request.form.get("student_number") or "").strip()
    name = (request.form.get("name") or "").strip()

    if not number:
        flash("The student number is needed — it is what identifies them.",
              "error")
        return redirect(url_for("courses.detail", course_id=course.id))

    if course.sections and not section_id:
        flash("Choose which section this student goes into.", "error")
        return redirect(url_for("courses.detail", course_id=course.id))

    if section_id:
        section = Section.query.filter_by(
            id=section_id, course_id=course.id
        ).first()
        if section is None:
            abort(404)

    existing = User.query.filter(
        func.lower(User.student_number) == number.lower()
    ).first()
    if existing is None and not name:
        flash(
            f"No student is on record with number {number}. Give the full name "
            f"as well and the account will be created.",
            "error",
        )
        return redirect(url_for("courses.detail", course_id=course.id))
    if existing is not None and existing.role != ROLE_STUDENT:
        flash(
            f"{number} belongs to a staff account, not a student.", "error"
        )
        return redirect(url_for("courses.detail", course_id=course.id))

    student, created = _find_or_create_student(
        number,
        name,
        (request.form.get("program") or "").strip(),
        (request.form.get("year_level") or "").strip(),
        (request.form.get("password") or "").strip(),
    )
    outcome = _enrol_student(course, student, section_id)

    log_action(
        current_user, "student_added", "course", course.id,
        f"{number} {outcome}{' (new account)' if created else ''}",
    )
    db.session.commit()

    label = student.name or number
    if outcome == "already":
        flash(f"{label} is already enrolled in this subject.", "info")
    elif outcome == "readmitted":
        flash(f"{label} was dropped and is now back on the list.", "success")
    elif outcome == "moved":
        flash(f"{label} was moved to the chosen section.", "success")
    elif created:
        flash(
            f"Created an account for {label} and enrolled them. The password "
            f"is {'the one you set' if request.form.get('password') else 'their student number'}, "
            f"and they must change it when they first sign in.",
            "success",
        )
    else:
        flash(f"{label} was already on record and is now enrolled.", "success")

    return redirect(
        url_for("courses.detail", course_id=course.id, section=section_id)
    )


@bp.route("/<int:course_id>/import-students", methods=["POST"])
@staff_required
def import_students(course_id: int):
    """Create accounts and enrol them from a pasted or uploaded CSV.

    Accepts ``student number, full name, program, year level`` with or without
    a header row -- which is how class lists actually arrive.
    """
    course = require_course(course_id)
    section_id = request.form.get("section_id", type=int)
    if course.sections and not section_id:
        flash("Choose which section these students go into.", "error")
        return redirect(url_for("courses.detail", course_id=course.id))

    raw = request.form.get("pasted") or ""
    upload = request.files.get("csv_file")
    if upload is not None and upload.filename:
        raw = upload.read().decode("utf-8-sig", "replace")
    if not raw.strip():
        flash("Paste the list, or choose a CSV file.", "error")
        return redirect(url_for("courses.detail", course_id=course.id))

    created = enrolled = skipped = 0
    problems: list[str] = []
    default_password = (request.form.get("default_password") or "").strip()

    reader = csv.reader(io.StringIO(raw.replace("\t", ",")))
    for line_no, row in enumerate(reader, start=1):
        cells = [cell.strip() for cell in row if cell is not None]
        cells = [c for c in cells if c != ""]
        if not cells:
            continue
        joined = " ".join(cells).lower()
        if line_no == 1 and ("student" in joined and "name" in joined):
            continue                      # header row

        if len(cells) == 1:
            problems.append(f"line {line_no}: need at least a number and a name")
            continue

        number, name = cells[0], cells[1]
        program = cells[2] if len(cells) > 2 else ""
        year_level = cells[3] if len(cells) > 3 else ""

        student, was_created = _find_or_create_student(
            number, name, program, year_level, default_password
        )
        if was_created:
            created += 1

        if _enrol_student(course, student, section_id) == "already":
            skipped += 1
        else:
            enrolled += 1

    log_action(
        current_user, "students_imported", "course", course.id,
        f"created={created} enrolled={enrolled}",
    )
    db.session.commit()

    message = f"{created} new account(s) created, {enrolled} enrolled"
    if skipped:
        message += f", {skipped} already in the list"
    message += "."
    if created and not default_password:
        message += " New accounts start with the student number as the password."
    flash(message, "success")
    if problems:
        flash("Some lines were skipped: " + "; ".join(problems[:6]), "warning")
    return redirect(url_for("courses.detail", course_id=course.id, section=section_id))


# --------------------------------------------------------------------------- #
# Performance tasks straight from the subject page
# --------------------------------------------------------------------------- #

@bp.route("/<int:course_id>/items", methods=["POST"])
@staff_required
def item_add(course_id: int):
    """Add a quiz, task, report or major exam without leaving this page."""
    course = require_course(course_id)
    component_id = request.form.get("component_id", type=int)
    component = GradeComponent.query.filter_by(
        id=component_id, course_id=course.id
    ).first()
    if component is None:
        flash("Choose which component this belongs under.", "error")
        return redirect(url_for("courses.detail", course_id=course.id))

    title = (request.form.get("title") or "").strip()
    if not title:
        flash("Give the activity a title.", "error")
        return redirect(url_for("courses.detail", course_id=course.id))

    max_points = request.form.get("max_points", type=float)
    if not max_points or max_points <= 0:
        flash("The highest possible score must be above zero.", "error")
        return redirect(url_for("courses.detail", course_id=course.id))

    date_given = None
    raw_date = request.form.get("date_given")
    if raw_date:
        try:
            date_given = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            date_given = None

    rubric_id = request.form.get("rubric_id", type=int) or None
    if rubric_id is not None:
        rubric = Rubric.query.get(rubric_id)
        if rubric is None:
            rubric_id = None
        elif rubric.is_template:
            rubric_id = _copy_template(rubric, course).id

    last = (
        db.session.query(func.max(GradeItem.order_index))
        .filter(GradeItem.component_id == component.id)
        .scalar()
        or 0
    )

    item = GradeItem(
        component_id=component.id,
        section_id=request.form.get("section_id", type=int) or None,
        title=title,
        item_type=request.form.get("item_type") or "task",
        max_points=max_points,
        weight_multiplier=request.form.get("weight_multiplier", type=float) or 1.0,
        date_given=date_given,
        rubric_id=rubric_id,
        instructions=(request.form.get("instructions") or "").strip(),
        drive_folder_url=(request.form.get("drive_folder_url") or "").strip(),
        accepts_submission=bool(request.form.get("accepts_submission")),
        order_index=last + 1,
    )
    db.session.add(item)
    log_action(current_user, "item_added", "grade_item", None,
               f"{course.code}: {title}")
    db.session.commit()

    flash(f"Added “{title}” under {component.name}.", "success")
    if item.rubric_id:
        return redirect(url_for("rubrics.grade", item_id=item.id))
    return redirect(url_for("gradebook.item", item_id=item.id))


def _copy_template(template: Rubric, course: Course) -> Rubric:
    """Templates are shared; copying keeps the instructor's edits local."""
    from ..models import RubricCriterion

    clone = Rubric(
        course_id=course.id,
        name=template.name,
        kind=template.kind,
        description=template.description,
        points_per_criterion=template.points_per_criterion,
        is_template=False,
    )
    db.session.add(clone)
    db.session.flush()
    for criterion in template.criteria:
        db.session.add(
            RubricCriterion(
                rubric_id=clone.id,
                title=criterion.title,
                description=criterion.description,
                max_points=criterion.max_points,
                order_index=criterion.order_index,
                levels_raw=criterion.levels_raw,
            )
        )
    return clone


@bp.route("/<int:course_id>/components", methods=["POST"])
@staff_required
def component_save(course_id: int):
    """Edit the component weights; they must add to 100 within each scope."""
    course = require_course(course_id)

    for component in course.components:
        field = f"weight_{component.id}"
        if field in request.form:
            component.weight_percent = request.form.get(field, type=float) or 0.0
        name_field = f"name_{component.id}"
        if request.form.get(name_field):
            component.name = request.form.get(name_field).strip()

    for scope, label in ((SCOPE_LECTURE, "Lecture"), (SCOPE_LABORATORY, "Laboratory")):
        members = [c for c in course.components if c.scope == scope]
        if not members:
            continue
        total = sum(c.weight_percent or 0.0 for c in members)
        if abs(total - 100.0) > 0.01:
            db.session.rollback()
            flash(
                f"{label} weights come to {total:g}%. They must total 100%.",
                "error",
            )
            return redirect(url_for("courses.detail", course_id=course.id))

    log_action(current_user, "components_saved", "course", course.id)
    db.session.commit()
    flash("Component weights saved.", "success")
    return redirect(url_for("courses.detail", course_id=course.id))


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #

@bp.route("/<int:course_id>/attendance", methods=["GET", "POST"])
@staff_required
def attendance(course_id: int):
    course = require_course(course_id)
    section_id = request.args.get("section", type=int)

    if request.method == "POST":
        raw_date = request.form.get("meeting_date") or ""
        try:
            meeting_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            meeting_date = date.today()

        post_section = request.form.get("section_id", type=int) or None
        session_row = AttendanceSession.query.filter_by(
            course_id=course.id, section_id=post_section, meeting_date=meeting_date
        ).first()
        if session_row is None:
            session_row = AttendanceSession(
                course_id=course.id, section_id=post_section, meeting_date=meeting_date
            )
            db.session.add(session_row)
            db.session.flush()
        session_row.topic = (request.form.get("topic") or "").strip()

        existing = {r.student_id: r for r in session_row.records}
        for student in course.roster(post_section):
            status = request.form.get(f"status_{student.id}")
            if not status:
                continue
            record = existing.get(student.id)
            if record is None:
                record = AttendanceRecord(
                    session_id=session_row.id, student_id=student.id
                )
                db.session.add(record)
            record.status = status
            record.note = (request.form.get(f"note_{student.id}") or "").strip()

        log_action(current_user, "attendance_saved", "course", course.id, str(meeting_date))
        db.session.commit()
        flash(f"Attendance for {meeting_date:%d %b %Y} saved.", "success")
        return redirect(
            url_for("courses.attendance", course_id=course.id, section=post_section)
        )

    sessions = (
        AttendanceSession.query.filter_by(course_id=course.id)
        .order_by(AttendanceSession.meeting_date.desc())
        .all()
    )
    if section_id:
        sessions = [s for s in sessions if s.section_id == section_id]

    students = course.roster(section_id)

    # A compact matrix, plus each student's attendance rate.
    matrix: dict[int, dict[int, str]] = {}
    for session_row in sessions:
        for record in session_row.records:
            matrix.setdefault(record.student_id, {})[session_row.id] = record.status

    summary = {}
    for student in students:
        marks = matrix.get(student.id, {})
        present = sum(1 for v in marks.values() if v in ("present", "late", "excused"))
        held = len(sessions)
        summary[student.id] = {
            "present": present,
            "absent": sum(1 for v in marks.values() if v == "absent"),
            "late": sum(1 for v in marks.values() if v == "late"),
            "rate": round(present / held * 100, 1) if held else None,
        }

    return render_template(
        "courses/attendance.html",
        course=course,
        sections=list(course.sections),
        section_id=section_id,
        students=students,
        sessions=sessions,
        matrix=matrix,
        summary=summary,
        statuses=AttendanceRecord.STATUSES,
        today=date.today().isoformat(),
    )


@bp.route("/<int:course_id>/attendance/<int:session_id>/delete", methods=["POST"])
@staff_required
def attendance_delete(course_id: int, session_id: int):
    course = require_course(course_id)
    session_row = AttendanceSession.query.filter_by(
        id=session_id, course_id=course.id
    ).first_or_404()
    label = f"{session_row.meeting_date:%d %b %Y}"
    db.session.delete(session_row)
    db.session.commit()
    flash(f"Attendance for {label} deleted.", "success")
    return redirect(url_for("courses.attendance", course_id=course.id))
