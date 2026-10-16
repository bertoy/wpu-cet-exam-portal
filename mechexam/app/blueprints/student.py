"""The student portal: progress per subject, submissions, account.

Progress is shown one tab per enrolled subject, which is what the instructor
asked for, and it respects the release gate: until the instructor publishes
grades for a subject, the student sees their individual marks but not a
computed standing.
"""
from __future__ import annotations

import os
import secrets

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user
from werkzeug.utils import secure_filename

from .. import drive as drive_mod
from ..extensions import db
from ..grading import compute_student
from ..models import (
    Attempt,
    ATTEMPT_SUBMITTED,
    Course,
    Enrollment,
    Exam,
    GradeComponent,
    GradeItem,
    RubricScore,
    Score,
    Submission,
    log_action,
    utcnow,
)
from ..security import student_required

bp = Blueprint("student", __name__)


def _my_courses() -> list[Course]:
    rows = (
        Enrollment.query.filter_by(student_id=current_user.id, is_dropped=False)
        .join(Course, Enrollment.course_id == Course.id)
        .order_by(Course.school_year.desc(), Course.semester, Course.code)
        .all()
    )
    return [row.course for row in rows if row.course is not None]


@bp.route("/progress")
@student_required
def progress():
    courses = _my_courses()
    selected_id = request.args.get("subject", type=int)
    selected = next((c for c in courses if c.id == selected_id), None) or (
        courses[0] if courses else None
    )

    payload = None
    if selected is not None:
        payload = _progress_for(selected)

    return render_template(
        "student/progress.html",
        courses=courses,
        selected=selected,
        payload=payload,
    )


def _progress_for(course: Course) -> dict:
    """Everything the student may see about one subject."""
    result = compute_student(course, current_user)

    items: list[dict] = []
    scores = {
        row.item_id: row
        for row in Score.query.join(GradeItem, Score.item_id == GradeItem.id)
        .join(GradeComponent, GradeItem.component_id == GradeComponent.id)
        .filter(
            GradeComponent.course_id == course.id,
            Score.student_id == current_user.id,
        )
        .all()
    }

    enrolment = Enrollment.query.filter_by(
        course_id=course.id, student_id=current_user.id
    ).first()
    my_section = enrolment.section_id if enrolment else None

    rubric_scores = {
        row.item_id: row
        for row in RubricScore.query.filter_by(student_id=current_user.id).all()
        if row.item_id
    }
    submissions = {
        row.item_id: row
        for row in Submission.query.filter_by(student_id=current_user.id).all()
    }

    for component in sorted(course.components, key=lambda c: (c.scope, c.order_index or 0)):
        for item in component.items:
            if not item.is_published:
                continue
            if item.item_type == "removal":
                continue
            if item.section_id and my_section and item.section_id != my_section:
                continue
            row = scores.get(item.id)
            items.append(
                {
                    "component": component,
                    "item": item,
                    "points": row.points if row is not None else None,
                    "remarks": (row.remarks or "") if row is not None else "",
                    "percent": round(row.points / item.max_points * 100, 2)
                    if row is not None and row.points is not None and item.max_points
                    else None,
                    "rubric": rubric_scores.get(item.id),
                    "submission": submissions.get(item.id),
                }
            )

    attempts = (
        Attempt.query.join(Exam, Attempt.exam_id == Exam.id)
        .filter(Exam.course_id == course.id, Attempt.student_id == current_user.id)
        .order_by(Attempt.submitted_at.desc().nullslast())
        .all()
    )

    pending = [
        item
        for item in (
            GradeItem.query.join(
                GradeComponent, GradeItem.component_id == GradeComponent.id
            )
            .filter(
                GradeComponent.course_id == course.id,
                GradeItem.accepts_submission.is_(True),
                GradeItem.is_published.is_(True),
            )
            .all()
        )
        if item.id not in submissions
    ]

    return {
        "result": result,
        "entries": items,
        "attempts": attempts,
        "pending_submissions": pending,
        "released": course.grades_released,
        "section": enrolment.section if enrolment else None,
    }


# --------------------------------------------------------------------------- #
# Submissions
# --------------------------------------------------------------------------- #

@bp.route("/submit/<int:item_id>", methods=["GET", "POST"])
@student_required
def submit(item_id: int):
    """Attach a file; it goes to Drive at once, with no manual step."""
    item = GradeItem.query.get_or_404(item_id)
    component = GradeComponent.query.get_or_404(item.component_id)
    course = Course.query.get_or_404(component.course_id)

    enrolment = Enrollment.query.filter_by(
        course_id=course.id, student_id=current_user.id, is_dropped=False
    ).first()
    if enrolment is None or not item.accepts_submission or not item.is_published:
        abort(404)

    existing = Submission.query.filter_by(
        item_id=item.id, student_id=current_user.id
    ).first()

    if request.method == "POST":
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("Choose a file to submit.", "error")
            return redirect(url_for("student.submit", item_id=item.id))

        data = upload.read()
        if not data:
            flash("That file is empty.", "error")
            return redirect(url_for("student.submit", item_id=item.id))

        # Keep a copy here first, so Drive trouble never loses the work.
        extension = os.path.splitext(upload.filename)[1][:12]
        stored_name = secure_filename(
            f"item{item.id}-s{current_user.id}-{secrets.token_hex(4)}{extension}"
        )
        directory = current_app.config["UPLOAD_DIR"] / "submissions"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / stored_name).write_bytes(data)

        row = existing
        if row is None:
            row = Submission(item_id=item.id, student_id=current_user.id)
            db.session.add(row)
        else:
            old = directory / (row.stored_path or "")
            try:
                if row.stored_path and old.is_file():
                    old.unlink()
            except OSError:
                pass

        row.filename = upload.filename[:300]
        row.stored_path = stored_name
        row.mime_type = upload.mimetype or "application/octet-stream"
        row.size_bytes = len(data)
        row.note = (request.form.get("note") or "").strip()
        row.submitted_at = utcnow()
        row.drive_error = ""

        if item.drive_folder_url:
            outcome = drive_mod.upload_bytes(
                data,
                filename=f"{current_user.student_number or current_user.username} — "
                         f"{current_user.name} — {upload.filename}",
                mime_type=row.mime_type,
                folder_url=item.drive_folder_url,
                description=f"{course.code} · {item.title}",
            )
            if outcome.ok:
                row.drive_file_id = outcome.file_id
                row.drive_url = outcome.url
            else:
                row.drive_error = outcome.error[:490]

        log_action(current_user, "submission", "grade_item", item.id, row.filename)
        db.session.commit()

        if row.drive_error:
            flash(
                "Your file was received and saved on the server. It could not be "
                "copied to the instructor's Drive folder, and they have been "
                "told why — you do not need to do anything.",
                "warning",
            )
        else:
            flash("Your file has been submitted.", "success")
        return redirect(url_for("student.progress", subject=course.id))

    return render_template(
        "student/submit.html", item=item, course=course, existing=existing
    )


@bp.route("/submission/<int:submission_id>/file")
def submission_file(submission_id: int):
    """The student who owns it, or the staff who teach the subject."""
    from flask_login import current_user as who

    if not who.is_authenticated:
        abort(404)

    row = Submission.query.get_or_404(submission_id)
    item = GradeItem.query.get_or_404(row.item_id)
    component = GradeComponent.query.get_or_404(item.component_id)

    allowed = row.student_id == who.id
    if not allowed and who.is_staff:
        from ..security import may_manage_course

        course = Course.query.get(component.course_id)
        allowed = course is not None and may_manage_course(course)
    if not allowed:
        abort(404)

    directory = current_app.config["UPLOAD_DIR"] / "submissions"
    if not row.stored_path or not (directory / row.stored_path).is_file():
        abort(404)
    return send_from_directory(
        directory,
        row.stored_path,
        as_attachment=False,
        download_name=row.filename or row.stored_path,
    )


@bp.route("/submission/<int:submission_id>/withdraw", methods=["POST"])
@student_required
def withdraw(submission_id: int):
    row = Submission.query.get_or_404(submission_id)
    if row.student_id != current_user.id:
        abort(404)

    directory = current_app.config["UPLOAD_DIR"] / "submissions"
    try:
        target = directory / (row.stored_path or "")
        if row.stored_path and target.is_file():
            target.unlink()
    except OSError:
        pass

    item_id = row.item_id
    db.session.delete(row)
    db.session.commit()
    flash(
        "Your submission has been withdrawn. Note that a copy already sent to "
        "the instructor's Drive folder stays with them.",
        "info",
    )
    return redirect(url_for("student.submit", item_id=item_id))


# --------------------------------------------------------------------------- #
# Account
# --------------------------------------------------------------------------- #

@bp.route("/account", methods=["GET", "POST"])
@student_required
def account():
    if request.method == "POST":
        current_user.contact = (request.form.get("contact") or "").strip()
        current_user.email = (request.form.get("email") or "").strip()
        current_user.theme = request.form.get("theme") or "system"
        db.session.commit()
        flash("Your details have been updated.", "success")
        return redirect(url_for("student.account"))

    courses = _my_courses()
    return render_template("student/account.html", courses=courses)


@bp.route("/history")
@student_required
def history():
    attempts = (
        Attempt.query.filter_by(student_id=current_user.id, status=ATTEMPT_SUBMITTED)
        .order_by(Attempt.submitted_at.desc())
        .all()
    )
    return render_template("student/history.html", attempts=attempts)
