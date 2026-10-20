"""Printable reports: the grade sheet, class list and per-student slip.

These are laid out for A4 paper with the university heading and signature
blocks, because the sheet that gets signed and filed is the point of the whole
exercise.  Printing is handled by the browser against a print stylesheet --
no PDF toolchain to install on the host.
"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, render_template, request


from ..grading import class_tally, compute_class, compute_student, removal_item
from ..models import (
    AttendanceSession,
    Enrollment,
    Exam,
    Score,
    User,
)
from ..security import require_course, staff_required

bp = Blueprint("reports", __name__)


@bp.route("/<int:course_id>/gradesheet")
@staff_required
def gradesheet(course_id: int):
    """The official sheet: A4 landscape, ready to print and sign."""
    course = require_course(course_id, manage=False)
    section_id = request.args.get("section", type=int)
    results = compute_class(course, section_id)

    sections = {s.id: s.name for s in course.sections}
    enrolments = {
        e.student_id: sections.get(e.section_id, "")
        for e in Enrollment.query.filter_by(course_id=course.id).all()
    }

    section_label = ""
    if section_id:
        section_label = sections.get(section_id, "")

    return render_template(
        "reports/gradesheet.html",
        course=course,
        results=results,
        tally=class_tally(results),
        sections=list(course.sections),
        section_id=section_id,
        section_label=section_label,
        enrolments=enrolments,
        removal=removal_item(course),
        printed_on=datetime.now(),
        show_components=request.args.get("detail") == "1",
    )


@bp.route("/<int:course_id>/report-of-grades")
@staff_required
def report_of_grades(course_id: int):
    """The registrar's own form, WPU-OSF-OSAS-OAR-09.

    This is the sheet that is signed and filed, so it reproduces the printed
    original rather than paraphrasing it: the same heading, the same six
    columns, the same signature block and the same form number. The portal's
    own grade sheet stays as it is — that one is for working, this one is for
    submitting.

    One programme per sheet, which is what a section is, so the sheet is
    printed per section unless the subject has none.
    """
    course = require_course(course_id, manage=False)
    section_id = request.args.get("section", type=int)
    results = compute_class(course, section_id)

    section = next((s for s in course.sections if s.id == section_id), None)

    # The programme: the section's own, or whatever the class agrees on.
    programme = section.program if section and section.program else ""
    if not programme:
        named = {s.student.program for s in results if s.student.program}
        programme = named.pop() if len(named) == 1 else ""

    instructor = course.owner
    signature_name = (request.args.get("signed_by") or "").strip()
    if not signature_name and instructor is not None:
        signature_name = (instructor.name or instructor.username)

    submitted_on = (request.args.get("submitted") or "").strip()

    # The registrar writes the term as "Summer AY 2025-2026", not the portal's
    # own "Summer, 2025-2026".
    term = f"{course.semester} AY {course.school_year}".strip()

    return render_template(
        "reports/report_of_grades.html",
        course=course,
        results=results,
        section=section,
        sections=list(course.sections),
        section_id=section_id,
        programme=programme,
        instructor=instructor,
        signature_name=signature_name.upper(),
        submitted_on=submitted_on or datetime.now().strftime("%B %-d, %Y"),
        term=term,
        rows_per_page=request.args.get("rows", type=int) or 22,
        printed_on=datetime.now(),
    )


@bp.route("/<int:course_id>/class-list/print")
@staff_required
def class_list_print(course_id: int):
    course = require_course(course_id, manage=False)
    section_id = request.args.get("section", type=int)
    students = course.roster(section_id)
    section = next((s for s in course.sections if s.id == section_id), None)

    return render_template(
        "reports/class_list.html",
        course=course,
        students=students,
        section=section,
        printed_on=datetime.now(),
        columns=request.args.get("columns", type=int) or 0,
    )


@bp.route("/<int:course_id>/slip/<int:student_id>")
@staff_required
def slip(course_id: int, student_id: int):
    """A single student's grade slip, with the full component breakdown."""
    course = require_course(course_id, manage=False)
    student = User.query.get_or_404(student_id)
    result = compute_student(course, student)

    enrolment = Enrollment.query.filter_by(
        course_id=course.id, student_id=student.id
    ).first()

    return render_template(
        "reports/slip.html",
        course=course,
        student=student,
        result=result,
        enrolment=enrolment,
        printed_on=datetime.now(),
    )


@bp.route("/<int:course_id>/attendance/print")
@staff_required
def attendance_print(course_id: int):
    course = require_course(course_id, manage=False)
    section_id = request.args.get("section", type=int)
    students = course.roster(section_id)

    sessions = (
        AttendanceSession.query.filter_by(course_id=course.id)
        .order_by(AttendanceSession.meeting_date)
        .all()
    )
    if section_id:
        sessions = [s for s in sessions if s.section_id == section_id]

    matrix: dict[int, dict[int, str]] = {}
    for session in sessions:
        for record in session.records:
            matrix.setdefault(record.student_id, {})[session.id] = record.status

    section = next((s for s in course.sections if s.id == section_id), None)
    return render_template(
        "reports/attendance.html",
        course=course,
        students=students,
        sessions=sessions,
        matrix=matrix,
        section=section,
        printed_on=datetime.now(),
    )


@bp.route("/<int:course_id>/summary")
@staff_required
def summary(course_id: int):
    """A one-page account of the subject: distribution, activities, exams."""
    course = require_course(course_id, manage=False)
    results = compute_class(course)

    # Grade distribution for the chart.
    buckets: dict[str, int] = {}
    for result in results:
        grade = result.effective_grade
        buckets[grade] = buckets.get(grade, 0) + 1
    ordered = sorted(
        buckets.items(),
        key=lambda kv: (kv[0] == "INC", kv[0] == "5.00", kv[0]),
    )

    percents = [r.final_percent for r in results if r.final_percent is not None]
    stats = {}
    if percents:
        ranked = sorted(percents)
        middle = len(ranked) // 2
        stats = {
            "count": len(percents),
            "mean": round(sum(percents) / len(percents), 2),
            "median": round(
                ranked[middle]
                if len(ranked) % 2
                else (ranked[middle - 1] + ranked[middle]) / 2,
                2,
            ),
            "high": max(percents),
            "low": min(percents),
        }

    components = []
    for component in sorted(course.components,
                            key=lambda c: (c.scope, c.order_index or 0)):
        entered = 0
        total_items = len(component.items)
        for item in component.items:
            if Score.query.filter(
                Score.item_id == item.id, Score.points.isnot(None)
            ).count():
                entered += 1
        # NEVER name a key "items": in Jinja, row.items would resolve to the
        # dict's own .items method instead of this value, and the template
        # would compare a number with a bound method.
        components.append(
            {"component": component, "item_count": total_items, "with_marks": entered}
        )

    exams = Exam.query.filter_by(course_id=course.id).order_by(Exam.created_at).all()

    return render_template(
        "reports/summary.html",
        course=course,
        results=results,
        tally=class_tally(results),
        distribution=ordered,
        stats=stats,
        components=components,
        exams=exams,
        printed_on=datetime.now(),
    )
