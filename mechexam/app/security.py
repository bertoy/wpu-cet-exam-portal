"""Roles, access decorators and subject visibility.

The rule the instructor was explicit about: an instructor sees *only* the
subjects assigned to them.  The previous build leaked unassigned subjects
because the visibility query also matched ``owner_id IS NULL``; that clause is
deliberately absent here, and every subject-scoped view goes through
``require_course`` rather than trusting an id from the URL.
"""
from __future__ import annotations

from functools import wraps

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user

from .models import (
    Course,
    Enrollment,
    ROLE_INSTRUCTOR,
    ROLE_STUDENT,
    ROLE_SUPERADMIN,
    Setting,
)


def _deny(message: str = "You do not have access to that page."):
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login", next=request.full_path))
    flash(message, "error")
    return redirect(url_for("dashboard.index"))


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login", next=request.full_path))
            if current_user.role not in roles:
                return _deny()
            return view(*args, **kwargs)
        return wrapper
    return decorator


superadmin_required = roles_required(ROLE_SUPERADMIN)
staff_required = roles_required(ROLE_SUPERADMIN, ROLE_INSTRUCTOR)
student_required = roles_required(ROLE_STUDENT)


def visible_courses(user=None, school_year: str | None = None,
                    semester: str | None = None, all_terms: bool = False):
    """Subjects this user may see, scoped to the current term by default."""
    user = user or current_user
    query = Course.query

    if not all_terms:
        year = school_year or Setting.get("school_year")
        term = semester or Setting.get("semester")
        if year:
            query = query.filter(Course.school_year == year)
        if term:
            query = query.filter(Course.semester == term)

    if getattr(user, "role", None) == ROLE_SUPERADMIN:
        pass
    elif getattr(user, "role", None) == ROLE_INSTRUCTOR:
        # Assigned subjects only.  No IS NULL fallback -- that was the leak.
        query = query.filter(Course.owner_id == user.id)
    elif getattr(user, "role", None) == ROLE_STUDENT:
        query = query.join(Enrollment, Enrollment.course_id == Course.id).filter(
            Enrollment.student_id == user.id,
            Enrollment.is_dropped.is_(False),
        )
    else:
        return query.filter(db_false())

    return query.order_by(Course.code)


def db_false():
    from sqlalchemy import false
    return false()


def may_manage_course(course: Course, user=None) -> bool:
    user = user or current_user
    if not getattr(user, "is_authenticated", False):
        return False
    if user.role == ROLE_SUPERADMIN:
        return True
    return user.role == ROLE_INSTRUCTOR and course.owner_id == user.id


def may_view_course(course: Course, user=None) -> bool:
    user = user or current_user
    if may_manage_course(course, user):
        return True
    if getattr(user, "role", None) == ROLE_STUDENT:
        return (
            Enrollment.query.filter_by(
                course_id=course.id, student_id=user.id, is_dropped=False
            ).first()
            is not None
        )
    return False


def require_course(course_id: int, manage: bool = True) -> Course:
    """Fetch a subject and enforce access in one step.

    Returning 404 rather than 403 for a subject the user may not manage keeps
    the existence of other instructors' subjects private.
    """
    course = Course.query.get(course_id)
    if course is None:
        abort(404)
    allowed = may_manage_course(course) if manage else may_view_course(course)
    if not allowed:
        abort(404)
    return course


def is_enrolled(course_id: int, student_id: int) -> bool:
    return (
        Enrollment.query.filter_by(
            course_id=course_id, student_id=student_id, is_dropped=False
        ).first()
        is not None
    )
