"""Rubrics: templates, editing, and grading against them.

Ten points per criterion is the default the instructor asked for, and the
grading page totals live as marks are entered so the final figure is never a
surprise.  The breakdown each student sees is the same one the instructor
filled in.
"""
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
from flask_login import current_user


from ..extensions import db
from ..models import (
    Answer,
    Attempt,
    GradeComponent,
    GradeItem,
    Rubric,
    RubricCriterion,
    RubricScore,
    Score,
    log_action,
    utcnow,
)
from ..security import require_course, staff_required, visible_courses

bp = Blueprint("rubrics", __name__)


# --------------------------------------------------------------------------- #
# Listing and creation
# --------------------------------------------------------------------------- #

@bp.route("/")
@staff_required
def index():
    courses = visible_courses().all()
    course_ids = [c.id for c in courses]
    mine = (
        Rubric.query.filter(Rubric.course_id.in_(course_ids or [-1]))
        .order_by(Rubric.name)
        .all()
    )
    templates = (
        Rubric.query.filter_by(is_template=True).order_by(Rubric.name).all()
    )
    return render_template(
        "gradebook/rubrics.html",
        mine=mine,
        templates=templates,
        courses=courses,
        kinds=Rubric.KINDS,
    )


@bp.route("/new", methods=["POST"])
@staff_required
def create():
    course = require_course(request.form.get("course_id", type=int))
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Give the rubric a name.", "error")
        return redirect(url_for("rubrics.index"))

    per_criterion = request.form.get("points_per_criterion", type=float) or 10.0
    rubric = Rubric(
        course_id=course.id,
        name=name,
        kind=request.form.get("kind") or "custom",
        description=(request.form.get("description") or "").strip(),
        points_per_criterion=per_criterion,
    )
    db.session.add(rubric)
    db.session.flush()

    titles = [t.strip() for t in (request.form.get("criteria") or "").splitlines()]
    titles = [t for t in titles if t]
    if not titles:
        titles = ["Criterion 1", "Criterion 2", "Criterion 3"]
    for index, title in enumerate(titles, start=1):
        db.session.add(
            RubricCriterion(
                rubric_id=rubric.id,
                title=title,
                max_points=per_criterion,
                order_index=index,
            )
        )

    log_action(current_user, "rubric_created", "rubric", rubric.id, name)
    db.session.commit()
    flash(f"Created the rubric “{name}”.", "success")
    return redirect(url_for("rubrics.edit", rubric_id=rubric.id))


@bp.route("/<int:rubric_id>/copy", methods=["POST"])
@staff_required
def copy(rubric_id: int):
    source = Rubric.query.get_or_404(rubric_id)
    course = require_course(request.form.get("course_id", type=int))

    clone = Rubric(
        course_id=course.id,
        name=(request.form.get("name") or source.name).strip(),
        kind=source.kind,
        description=source.description,
        points_per_criterion=source.points_per_criterion,
        is_template=False,
    )
    db.session.add(clone)
    db.session.flush()
    for criterion in source.criteria:
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
    db.session.commit()
    flash(f"Copied into {course.code} as “{clone.name}”.", "success")
    return redirect(url_for("rubrics.edit", rubric_id=clone.id))


def _require_rubric(rubric_id: int, manage: bool = True) -> Rubric:
    rubric = Rubric.query.get(rubric_id)
    if rubric is None:
        abort(404)
    if rubric.is_template:
        if manage and not current_user.is_superadmin:
            abort(404)
        return rubric
    if rubric.course_id:
        require_course(rubric.course_id, manage=manage)
    return rubric


@bp.route("/<int:rubric_id>", methods=["GET", "POST"])
@staff_required
def edit(rubric_id: int):
    rubric = _require_rubric(rubric_id)

    if request.method == "POST":
        action = request.form.get("action")

        if action == "delete":
            used = RubricScore.query.filter_by(rubric_id=rubric.id).count()
            if used and request.form.get("confirm") != "DELETE":
                flash(
                    f"This rubric has been used to mark {used} piece(s) of work. "
                    f"Type DELETE to confirm.",
                    "error",
                )
                return redirect(url_for("rubrics.edit", rubric_id=rubric.id))
            course_id = rubric.course_id
            db.session.delete(rubric)
            db.session.commit()
            flash("Rubric deleted.", "success")
            return redirect(
                url_for("courses.detail", course_id=course_id)
                if course_id
                else url_for("rubrics.index")
            )

        if action == "add_criterion":
            last = max([c.order_index or 0 for c in rubric.criteria] or [0])
            db.session.add(
                RubricCriterion(
                    rubric_id=rubric.id,
                    title=(request.form.get("title") or "New criterion").strip(),
                    description=(request.form.get("description") or "").strip(),
                    max_points=request.form.get("max_points", type=float)
                    or rubric.points_per_criterion,
                    order_index=last + 1,
                )
            )
            db.session.commit()
            flash("Criterion added.", "success")
            return redirect(url_for("rubrics.edit", rubric_id=rubric.id))

        if action == "delete_criterion":
            criterion = RubricCriterion.query.filter_by(
                id=request.form.get("criterion_id", type=int), rubric_id=rubric.id
            ).first_or_404()
            db.session.delete(criterion)
            db.session.commit()
            flash("Criterion removed.", "success")
            return redirect(url_for("rubrics.edit", rubric_id=rubric.id))

        # Save the whole rubric.
        rubric.name = (request.form.get("name") or rubric.name).strip()
        rubric.description = (request.form.get("description") or "").strip()
        rubric.kind = request.form.get("kind") or rubric.kind

        new_per = request.form.get("points_per_criterion", type=float)
        apply_all = bool(request.form.get("apply_to_all"))
        if new_per and new_per > 0:
            rubric.points_per_criterion = new_per

        for criterion in rubric.criteria:
            title = request.form.get(f"title_{criterion.id}")
            if title is not None and title.strip():
                criterion.title = title.strip()
            criterion.description = (
                request.form.get(f"description_{criterion.id}") or ""
            ).strip()
            if apply_all and new_per:
                criterion.max_points = new_per
            else:
                points = request.form.get(f"points_{criterion.id}", type=float)
                if points and points > 0:
                    criterion.max_points = points
            order = request.form.get(f"order_{criterion.id}", type=int)
            if order:
                criterion.order_index = order

        log_action(current_user, "rubric_saved", "rubric", rubric.id)
        db.session.commit()
        flash("Rubric saved.", "success")
        return redirect(url_for("rubrics.edit", rubric_id=rubric.id))

    return render_template(
        "gradebook/rubric_edit.html", rubric=rubric, kinds=Rubric.KINDS
    )


# --------------------------------------------------------------------------- #
# Grading a performance task against its rubric
# --------------------------------------------------------------------------- #

@bp.route("/grade/item/<int:item_id>", methods=["GET", "POST"])
@staff_required
def grade(item_id: int):
    item = GradeItem.query.get_or_404(item_id)
    component = GradeComponent.query.get_or_404(item.component_id)
    course = require_course(component.course_id)

    if item.rubric is None:
        flash("This activity has no rubric attached.", "error")
        return redirect(url_for("gradebook.item", item_id=item.id))

    rubric = item.rubric
    section_id = request.args.get("section", type=int) or item.section_id
    students = course.roster(section_id)

    if request.method == "POST":
        student_id = request.form.get("student_id", type=int)
        student = next((s for s in students if s.id == student_id), None)
        if student is None:
            abort(400)

        scores: dict[str, float] = {}
        total = 0.0
        for criterion in rubric.criteria:
            value = request.form.get(f"criterion_{criterion.id}", type=float)
            if value is None:
                continue
            value = max(0.0, min(value, criterion.max_points or 0.0))
            scores[str(criterion.id)] = value
            total += value

        row = RubricScore.query.filter_by(
            rubric_id=rubric.id, item_id=item.id, student_id=student.id
        ).first()
        if row is None:
            row = RubricScore(
                rubric_id=rubric.id, item_id=item.id, student_id=student.id
            )
            db.session.add(row)
        row.scores = scores
        row.total = round(total, 2)
        row.comment = (request.form.get("comment") or "").strip()
        row.graded_by_id = current_user.id

        # The rubric total becomes the mark, rescaled if the activity's
        # maximum differs from the rubric's.
        rubric_total = rubric.total_points or 0.0
        points = total
        if rubric_total > 0 and item.max_points and abs(rubric_total - item.max_points) > 0.01:
            points = round(total / rubric_total * item.max_points, 2)

        score_row = Score.query.filter_by(item_id=item.id, student_id=student.id).first()
        if score_row is None:
            score_row = Score(item_id=item.id, student_id=student.id)
            db.session.add(score_row)
        if not score_row.is_override:
            score_row.points = points
            score_row.graded_by_id = current_user.id

        log_action(current_user, "rubric_graded", "grade_item", item.id,
                   f"student={student.id} total={total}")
        db.session.commit()
        flash(
            f"Marked {student.display_name}: {total:g} of "
            f"{rubric_total:g} on the rubric"
            + (f", recorded as {points:g}." if abs(points - total) > 0.01 else "."),
            "success",
        )

        nxt = request.form.get("next_student_id", type=int)
        if nxt:
            return redirect(
                url_for("rubrics.grade", item_id=item.id, section=section_id,
                        student=nxt)
            )
        return redirect(url_for("rubrics.grade", item_id=item.id, section=section_id))

    existing = {
        row.student_id: row
        for row in RubricScore.query.filter_by(rubric_id=rubric.id, item_id=item.id).all()
    }
    selected_id = request.args.get("student", type=int)
    selected = next((s for s in students if s.id == selected_id), None)
    if selected is None:
        selected = next((s for s in students if s.id not in existing), None)
    if selected is None and students:
        selected = students[0]

    order = [s.id for s in students]
    next_id = None
    if selected is not None and selected.id in order:
        position = order.index(selected.id)
        if position + 1 < len(order):
            next_id = order[position + 1]

    submissions = {}
    if item.accepts_submission:
        from ..models import Submission

        submissions = {
            row.student_id: row
            for row in Submission.query.filter_by(item_id=item.id).all()
        }

    return render_template(
        "gradebook/rubric_grade.html",
        item=item,
        rubric=rubric,
        course=course,
        students=students,
        existing=existing,
        selected=selected,
        next_id=next_id,
        sections=list(course.sections),
        section_id=section_id,
        submissions=submissions,
    )


@bp.route("/grade/answer/<int:answer_id>", methods=["GET", "POST"])
@staff_required
def grade_answer(answer_id: int):
    """Mark one essay answer against a rubric."""
    answer = Answer.query.get_or_404(answer_id)
    attempt = Attempt.query.get_or_404(answer.attempt_id)
    exam = attempt.exam
    require_course(exam.course_id)

    rubric = answer.question.rubric if answer.question else None
    if rubric is None:
        flash("That question has no rubric attached.", "error")
        return redirect(url_for("exams.attempt_detail", attempt_id=attempt.id))

    if request.method == "POST":
        scores: dict[str, float] = {}
        total = 0.0
        for criterion in rubric.criteria:
            value = request.form.get(f"criterion_{criterion.id}", type=float)
            if value is None:
                continue
            value = max(0.0, min(value, criterion.max_points or 0.0))
            scores[str(criterion.id)] = value
            total += value

        row = RubricScore.query.filter_by(
            rubric_id=rubric.id, answer_id=answer.id, student_id=attempt.student_id
        ).first()
        if row is None:
            row = RubricScore(
                rubric_id=rubric.id,
                answer_id=answer.id,
                student_id=attempt.student_id,
            )
            db.session.add(row)
        row.scores = scores
        row.total = round(total, 2)
        row.comment = (request.form.get("comment") or "").strip()
        row.graded_by_id = current_user.id

        rubric_total = rubric.total_points or 0.0
        points = total
        if rubric_total > 0 and answer.max_points and abs(rubric_total - answer.max_points) > 0.01:
            points = round(total / rubric_total * answer.max_points, 2)

        answer.points_awarded = points
        answer.feedback = row.comment
        answer.needs_manual = False
        answer.graded_by_id = current_user.id
        answer.graded_at = utcnow()

        attempt.score = round(
            sum((a.points_awarded or 0.0) for a in attempt.answers), 4
        )
        attempt.manual_pending = sum(1 for a in attempt.answers if a.needs_manual)

        log_action(current_user, "essay_rubric_graded", "answer", answer.id)
        db.session.commit()

        from .exams import _sync_to_gradebook

        _sync_to_gradebook(exam)
        db.session.commit()

        flash(f"Marked: {points:g} of {answer.max_points:g}.", "success")
        return redirect(url_for("exams.attempt_detail", attempt_id=attempt.id))

    existing = RubricScore.query.filter_by(
        rubric_id=rubric.id, answer_id=answer.id
    ).first()

    return render_template(
        "gradebook/rubric_answer.html",
        answer=answer,
        attempt=attempt,
        exam=exam,
        rubric=rubric,
        existing=existing,
    )
