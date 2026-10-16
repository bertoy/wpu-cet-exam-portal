"""Announcements to a class or section, and private messages to one student."""
from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import or_

from ..extensions import db
from ..models import (
    Course,
    Enrollment,
    Message,
    MessageRead,
    User,
    log_action,
)
from ..security import may_manage_course, require_course, visible_courses

bp = Blueprint("messages", __name__)


def _visible_messages(user) -> list[Message]:
    if user.is_student:
        course_ids = [
            e.course_id
            for e in Enrollment.query.filter_by(student_id=user.id, is_dropped=False).all()
        ]
        return (
            Message.query.filter(
                or_(
                    Message.recipient_id == user.id,
                    db.and_(
                        Message.is_announcement.is_(True),
                        Message.course_id.in_(course_ids or [-1]),
                    ),
                )
            )
            .order_by(Message.is_pinned.desc(), Message.created_at.desc())
            .limit(200)
            .all()
        )

    course_ids = [c.id for c in visible_courses(user, all_terms=True).all()]
    return (
        Message.query.filter(
            or_(
                Message.sender_id == user.id,
                Message.recipient_id == user.id,
                Message.course_id.in_(course_ids or [-1]),
            )
        )
        .order_by(Message.is_pinned.desc(), Message.created_at.desc())
        .limit(200)
        .all()
    )


@bp.route("/")
@login_required
def index():
    listing = _visible_messages(current_user)
    read_ids = {
        row.message_id for row in MessageRead.query.filter_by(user_id=current_user.id).all()
    }
    return render_template(
        "messages/index.html",
        listing=listing,
        read_ids=read_ids,
        courses=visible_courses().all() if current_user.is_staff else [],
    )


@bp.route("/<int:message_id>")
@login_required
def detail(message_id: int):
    message = Message.query.get_or_404(message_id)

    allowed = message.sender_id == current_user.id or message.recipient_id == current_user.id
    if not allowed and message.is_announcement and message.course_id:
        if current_user.is_student:
            allowed = (
                Enrollment.query.filter_by(
                    course_id=message.course_id,
                    student_id=current_user.id,
                    is_dropped=False,
                ).first()
                is not None
            )
        else:
            course = Course.query.get(message.course_id)
            allowed = course is not None and may_manage_course(course)
    if not allowed:
        abort(404)

    if MessageRead.query.filter_by(
        message_id=message.id, user_id=current_user.id
    ).first() is None:
        db.session.add(MessageRead(message_id=message.id, user_id=current_user.id))
        db.session.commit()

    recipients = 0
    if message.is_announcement and message.course_id:
        course = Course.query.get(message.course_id)
        if course is not None:
            recipients = len(course.roster(message.section_id))

    return render_template(
        "messages/detail.html", message=message, recipients=recipients
    )


@bp.route("/compose", methods=["GET", "POST"])
@login_required
def compose():
    """Staff write announcements or private notes; students reply privately."""
    if request.method == "POST":
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        if not body:
            flash("The message is empty.", "error")
            return redirect(url_for("messages.compose"))

        kind = request.form.get("kind") or "private"

        if kind == "announcement":
            if not current_user.is_staff:
                abort(403)
            course = require_course(request.form.get("course_id", type=int))
            section_id = request.form.get("section_id", type=int) or None
            if section_id and not any(s.id == section_id for s in course.sections):
                abort(400)

            message = Message(
                sender_id=current_user.id,
                course_id=course.id,
                section_id=section_id,
                subject=subject or f"Announcement — {course.code}",
                body=body,
                is_announcement=True,
                is_pinned=bool(request.form.get("is_pinned")),
            )
            db.session.add(message)
            log_action(current_user, "announcement_sent", "course", course.id)
            db.session.commit()

            count = len(course.roster(section_id))
            flash(
                f"Announcement posted to {course.code}"
                + (
                    f", section {next(s.name for s in course.sections if s.id == section_id)}"
                    if section_id
                    else ""
                )
                + f" — {count} student(s) will see it.",
                "success",
            )
            return redirect(url_for("messages.index"))

        recipient_id = request.form.get("recipient_id", type=int)
        recipient = User.query.get(recipient_id) if recipient_id else None
        if recipient is None:
            flash("Choose who the message is for.", "error")
            return redirect(url_for("messages.compose"))

        if current_user.is_student:
            # A student may only write to staff who teach them.
            teachers = {
                c.owner_id
                for c in visible_courses(current_user).all()
                if c.owner_id
            }
            if recipient.id not in teachers and not recipient.is_superadmin:
                flash("You can only message your own instructors.", "error")
                return redirect(url_for("messages.compose"))

        message = Message(
            sender_id=current_user.id,
            recipient_id=recipient.id,
            course_id=request.form.get("course_id", type=int) or None,
            subject=subject or "Message",
            body=body,
        )
        db.session.add(message)
        log_action(current_user, "message_sent", "user", recipient.id)
        db.session.commit()
        flash(f"Message sent to {recipient.display_name}.", "success")
        return redirect(url_for("messages.index"))

    courses = visible_courses().all()
    preselect_course = request.args.get("course", type=int)
    preselect_student = request.args.get("student", type=int)

    people: list[User] = []
    if current_user.is_staff:
        student_ids = {
            e.student_id
            for e in Enrollment.query.filter(
                Enrollment.course_id.in_([c.id for c in courses] or [-1]),
                Enrollment.is_dropped.is_(False),
            ).all()
        }
        if student_ids:
            people = (
                User.query.filter(User.id.in_(student_ids)).order_by(User.name).all()
            )
    else:
        teacher_ids = {c.owner_id for c in courses if c.owner_id}
        if teacher_ids:
            people = (
                User.query.filter(User.id.in_(teacher_ids)).order_by(User.name).all()
            )

    return render_template(
        "messages/compose.html",
        courses=courses,
        people=people,
        preselect_course=preselect_course,
        preselect_student=preselect_student,
        default_kind=request.args.get("kind", "announcement"
                                      if current_user.is_staff else "private"),
    )


@bp.route("/<int:message_id>/delete", methods=["POST"])
@login_required
def delete(message_id: int):
    message = Message.query.get_or_404(message_id)
    if message.sender_id != current_user.id and not current_user.is_superadmin:
        abort(403)
    MessageRead.query.filter_by(message_id=message.id).delete(synchronize_session=False)
    db.session.delete(message)
    db.session.commit()
    flash("Message deleted.", "success")
    return redirect(url_for("messages.index"))


@bp.route("/read-all", methods=["POST"])
@login_required
def read_all():
    listing = _visible_messages(current_user)
    read_ids = {
        row.message_id for row in MessageRead.query.filter_by(user_id=current_user.id).all()
    }
    for message in listing:
        if message.id not in read_ids:
            db.session.add(
                MessageRead(message_id=message.id, user_id=current_user.id)
            )
    db.session.commit()
    return redirect(url_for("messages.index"))
