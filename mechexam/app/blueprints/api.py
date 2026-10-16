"""Small JSON endpoints used by the pages themselves."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify
from flask_login import login_required


from ..models import (
    Attempt,
    ATTEMPT_IN_PROGRESS,
    ATTEMPT_SUBMITTED,
    Exam,
)
from ..security import require_course, staff_required

bp = Blueprint("api", __name__)


@bp.route("/clock")
def clock():
    """Authoritative server time, so every exam clock agrees."""
    now = datetime.now()
    return jsonify(
        {
            "iso": now.isoformat(timespec="seconds"),
            "time": now.strftime("%I:%M:%S %p"),
            "date": now.strftime("%A, %d %B %Y"),
            "epoch": int(now.timestamp()),
        }
    )


@bp.route("/exam/<int:exam_id>/monitor.json")
@staff_required
def exam_monitor(exam_id: int):
    """Live state for the monitor page: who is in, who has finished."""
    exam = Exam.query.get_or_404(exam_id)
    require_course(exam.course_id)

    roster = exam.course.roster(exam.section_id)
    attempts = {a.student_id: a for a in Attempt.query.filter_by(exam_id=exam.id).all()}

    rows = []
    for student in roster:
        attempt = attempts.get(student.id)
        rows.append(
            {
                "student_id": student.id,
                "name": student.name,
                "number": student.student_number or "",
                "status": attempt.status if attempt else "not_started",
                "answered": len({a.question_id for a in attempt.answers}) if attempt else 0,
                "score": attempt.effective_score if attempt and attempt.is_submitted else None,
                "total": attempt.total_points if attempt else None,
                "events": len(attempt.events) if attempt else 0,
                "auto": bool(attempt.auto_submitted) if attempt else False,
                "last_seen": attempt.last_seen_at.strftime("%H:%M:%S")
                if attempt and attempt.last_seen_at
                else "",
                "attempt_id": attempt.id if attempt else None,
            }
        )

    return jsonify(
        {
            "open": bool(exam.is_open and not exam.is_expired),
            "seconds_remaining": exam.seconds_remaining,
            "closes_at": exam.closes_at.isoformat() if exam.closes_at else None,
            "questions": len(exam.questions),
            "points": exam.total_points,
            "rows": rows,
            "counts": {
                "not_started": sum(1 for r in rows if r["status"] == "not_started"),
                "in_progress": sum(1 for r in rows if r["status"] == ATTEMPT_IN_PROGRESS),
                "submitted": sum(1 for r in rows if r["status"] == ATTEMPT_SUBMITTED),
            },
        }
    )


@bp.route("/exam/<int:exam_id>/expire-check", methods=["POST"])
@login_required
def expire_check(exam_id: int):
    """Any client may ask; only an actually-expired exam is acted on.

    This is what guarantees the exam closes on time even if nobody is watching
    the monitor page -- the first client to notice triggers the close.
    """
    exam = Exam.query.get_or_404(exam_id)
    if not exam.is_expired:
        return jsonify({"expired": False, "seconds_remaining": exam.seconds_remaining})

    from .taking import _expire

    _expire(exam)
    return jsonify({"expired": True})


@bp.route("/katex/check")
def katex_check():
    """Confirms the bundled equation renderer is present, not a CDN."""
    import os

    from flask import current_app

    base = os.path.join(current_app.root_path, "static", "vendor", "katex")
    return jsonify(
        {
            "css": os.path.exists(os.path.join(base, "katex.min.css")),
            "js": os.path.exists(os.path.join(base, "katex.min.js")),
            "fonts": len(
                [
                    f
                    for f in os.listdir(os.path.join(base, "fonts"))
                    if f.endswith(".woff2")
                ]
            )
            if os.path.isdir(os.path.join(base, "fonts"))
            else 0,
        }
    )
