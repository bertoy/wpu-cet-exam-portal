"""The landing page, one per role."""
from __future__ import annotations

from flask import Blueprint, render_template, send_from_directory, current_app, jsonify
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (
    Attempt,
    ATTEMPT_SUBMITTED,
    Enrollment,
    Exam,
    GradeItem,
    Message,
    Setting,
    Submission,
    User,
    ROLE_INSTRUCTOR,
    ROLE_STUDENT,
    utcnow,
)
from ..security import visible_courses

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def index():
    courses = visible_courses().all()

    if current_user.is_student:
        return _student_dashboard(courses)
    return _staff_dashboard(courses)


def _staff_dashboard(courses):
    course_ids = [c.id for c in courses]

    open_exams = (
        Exam.query.filter(Exam.course_id.in_(course_ids or [-1]), Exam.is_open.is_(True))
        .order_by(Exam.opened_at.desc())
        .all()
        if course_ids
        else []
    )

    # Anything waiting on the instructor's own judgement.
    pending_essays = 0
    if course_ids:
        exam_ids = [
            row.id for row in Exam.query.filter(Exam.course_id.in_(course_ids)).all()
        ]
        if exam_ids:
            pending_essays = (
                db.session.query(db.func.coalesce(db.func.sum(Attempt.manual_pending), 0))
                .filter(Attempt.exam_id.in_(exam_ids),
                        Attempt.status == ATTEMPT_SUBMITTED)
                .scalar()
                or 0
            )

    headcount = 0
    if course_ids:
        headcount = (
            Enrollment.query.filter(
                Enrollment.course_id.in_(course_ids), Enrollment.is_dropped.is_(False)
            ).count()
        )

    recent_submissions = []
    if course_ids:
        from ..models import GradeComponent

        recent_submissions = (
            Submission.query.join(GradeItem, Submission.item_id == GradeItem.id)
            .join(GradeComponent, GradeItem.component_id == GradeComponent.id)
            .filter(GradeComponent.course_id.in_(course_ids))
            .order_by(Submission.submitted_at.desc())
            .limit(6)
            .all()
        )

    stats = {
        "subjects": len(courses),
        "students": headcount,
        "open_exams": len(open_exams),
        "pending_essays": int(pending_essays),
    }

    if current_user.is_superadmin:
        stats["instructors"] = User.query.filter_by(role=ROLE_INSTRUCTOR).count()
        stats["all_students"] = User.query.filter_by(role=ROLE_STUDENT).count()

    announcements = (
        Message.query.filter(Message.sender_id == current_user.id)
        .order_by(Message.created_at.desc())
        .limit(5)
        .all()
    )

    return render_template(
        "dashboard/staff.html",
        courses=courses,
        open_exams=open_exams,
        stats=stats,
        announcements=announcements,
        recent_submissions=recent_submissions,
    )


def _student_dashboard(courses):
    now = utcnow()
    course_ids = [c.id for c in courses]

    open_exams = []
    if course_ids:
        candidates = (
            Exam.query.filter(Exam.course_id.in_(course_ids), Exam.is_open.is_(True))
            .order_by(Exam.opened_at.desc())
            .all()
        )
        my_sections = {
            e.course_id: e.section_id
            for e in Enrollment.query.filter_by(
                student_id=current_user.id, is_dropped=False
            ).all()
        }
        for exam in candidates:
            if exam.section_id and my_sections.get(exam.course_id) != exam.section_id:
                continue
            attempt = Attempt.query.filter_by(
                exam_id=exam.id, student_id=current_user.id
            ).first()
            if attempt is not None and attempt.is_submitted:
                continue
            open_exams.append(exam)

    recent = (
        Attempt.query.filter_by(student_id=current_user.id, status=ATTEMPT_SUBMITTED)
        .order_by(Attempt.submitted_at.desc())
        .limit(6)
        .all()
    )

    return render_template(
        "dashboard/student.html",
        courses=courses,
        open_exams=open_exams,
        recent=recent,
        now=now,
    )


# --------------------------------------------------------------------------- #
# PWA plumbing
# --------------------------------------------------------------------------- #

@bp.route("/manifest.webmanifest")
def manifest():
    from ..assets import asset_url

    data = {
        "name": current_app.config["APP_NAME"],
        "short_name": "WPU Exams",
        "description": "Examination and gradebook portal for the "
                       "College of Engineering and Technology.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0b1f3a",
        "theme_color": "#0b1f3a",
        "icons": [
            {"src": asset_url("img/icon-192.png"), "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": asset_url("img/icon-512.png"), "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": asset_url("img/icon-maskable.png"), "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "My progress", "url": "/my/progress"},
            {"name": "Messages", "url": "/messages/"},
        ],
    }
    response = jsonify(data)
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.route("/service-worker.js")
def service_worker():
    """Served from the root so its scope covers the whole app."""
    directory = current_app.root_path + "/static/js"
    response = send_from_directory(directory, "service-worker.js")
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.route("/offline")
def offline():
    return render_template("errors/offline.html")


@bp.route("/healthz")
def healthz():
    try:
        db.session.execute(db.text("SELECT 1"))
        return {"status": "ok", "term": Setting.get("school_year")}, 200
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}, 503
