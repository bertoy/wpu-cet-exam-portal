"""Exam authoring: questions of every type, the equation editor, banks.

The multi-part parser here is deliberately order-tolerant.  The previous build
crashed with ``could not convert string to float: 'num'`` because it assumed a
fixed column order and ran ``float()`` on whatever sat in position four.  This
version inspects each extra field and decides what it is, so
``a | Find T2 | 318.5 | num | 5`` and ``a | Find T2 | 318.5 | 5 | num`` both
work, and anything it cannot read is reported instead of raising.
"""
from __future__ import annotations

import os
import random
import re
import secrets
from datetime import timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user
from sqlalchemy import func, or_
from werkzeug.utils import secure_filename

from ..extensions import db
from ..marking import mark_attempt, rescore_exam
from ..models import (
    Attempt,
    ATTEMPT_IN_PROGRESS,
    ATTEMPT_SUBMITTED,
    Exam,
    EXIT_STRICT,
    GradeComponent,
    GradeItem,
    Q_ESSAY,
    Q_MATCHING,
    Q_MCQ,
    Q_MULTIPART,
    Q_NUMERIC,
    Q_SHORT,
    Q_TRUEFALSE,
    Question,
    QuestionBank,
    Rubric,
    RubricScore,
    Score,
    Setting,
    SubQuestion,
    log_action,
    utcnow,
)
from ..question_io import (
    normalise_part_type,
    parse_parts_bulk,
    parse_questions_csv,
    questions_to_csv,
    template_csv,
)
from ..security import require_course, staff_required, visible_courses

bp = Blueprint("exams", __name__)

ALLOWED_FIGURES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


# --------------------------------------------------------------------------- #
# Listing and creation
# --------------------------------------------------------------------------- #

@bp.route("/")
@staff_required
def index():
    course_ids = [c.id for c in visible_courses().all()]
    exams = (
        Exam.query.filter(Exam.course_id.in_(course_ids or [-1]))
        .order_by(Exam.is_open.desc(), Exam.created_at.desc())
        .all()
    )
    rows = []
    for exam in exams:
        rows.append(
            {
                "exam": exam,
                "questions": len(exam.questions),
                "points": exam.total_points,
                "submitted": Attempt.query.filter_by(
                    exam_id=exam.id, status=ATTEMPT_SUBMITTED
                ).count(),
                "in_progress": Attempt.query.filter_by(
                    exam_id=exam.id, status=ATTEMPT_IN_PROGRESS
                ).count(),
            }
        )
    return render_template("exams/index.html", rows=rows)


@bp.route("/new", methods=["GET", "POST"])
@staff_required
def create():
    courses = visible_courses().all()
    if not courses:
        flash(
            "You have no subjects assigned for this term, so there is nothing "
            "to create an exam under.",
            "warning",
        )
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        course_id = request.form.get("course_id", type=int)
        course = require_course(course_id)       # enforces assignment

        title = (request.form.get("title") or "").strip()
        if not title:
            flash("Give the exam a title.", "error")
            return redirect(url_for("exams.create"))

        duration = request.form.get("duration_minutes", type=int) or 60
        exam = Exam(
            course_id=course.id,
            section_id=request.form.get("section_id", type=int) or None,
            title=title,
            category=request.form.get("category") or "Quiz",
            topic=(request.form.get("topic") or "").strip(),
            instructions=(request.form.get("instructions") or "").strip(),
            duration_minutes=max(1, duration),
            access_code=(request.form.get("access_code") or "").strip()
            or secrets.token_hex(3).upper(),
            shuffle_questions=bool(request.form.get("shuffle_questions")),
            shuffle_options=bool(request.form.get("shuffle_options")),
            exit_mode=request.form.get("exit_mode") or EXIT_STRICT,
            exit_grace_seconds=request.form.get("exit_grace_seconds", type=int) or 180,
            lock_grace_seconds=_bounded(
                request.form.get("lock_grace_seconds", type=int), 90, 5, 600
            ),
            exit_allowance=_bounded(
                request.form.get("exit_allowance", type=int), 1, 0, 10
            ),
            idle_seconds=_bounded(
                request.form.get("idle_seconds", type=int), 0, 0, 3600
            ),
            block_paste=bool(request.form.get("block_paste")),
            watermark=bool(request.form.get("watermark")),
            hide_when_unfocused=bool(request.form.get("hide_when_unfocused")),
            allow_calculator=bool(request.form.get("allow_calculator")),
            allow_backtrack=bool(request.form.get("allow_backtrack")),
            one_question_per_page=bool(request.form.get("one_question_per_page")),
            show_score=bool(request.form.get("show_score")),
            show_answers=bool(request.form.get("show_answers")),
            bank_draw_count=request.form.get("bank_draw_count", type=int) or 0,
            link_item_id=request.form.get("link_item_id", type=int) or None,
            school_year=course.school_year,
            semester=course.semester,
            created_by_id=current_user.id,
        )
        db.session.add(exam)
        log_action(current_user, "exam_created", "exam", None, f"{course.code}: {title}")
        db.session.commit()
        flash("Exam created. Add the questions next.", "success")
        return redirect(url_for("exams.edit", exam_id=exam.id))

    preselect = request.args.get("course", type=int)
    return render_template(
        "exams/create.html",
        courses=courses,
        preselect=preselect,
        categories=Exam.CATEGORIES,
        exit_modes=Exam.EXIT_MODES,
        gradebook_items=_linkable_items(preselect) if preselect else [],
    )


def _linkable_items(course_id: int):
    return (
        GradeItem.query.join(GradeComponent, GradeItem.component_id == GradeComponent.id)
        .filter(GradeComponent.course_id == course_id)
        .order_by(GradeComponent.order_index, GradeItem.order_index)
        .all()
    )


_ROMAN = (
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)


def _roman(number: int) -> str:
    out = []
    for value, glyph in _ROMAN:
        while number >= value:
            out.append(glyph)
            number -= value
    return "".join(out)


def _key_grid(rows: list[dict], per_column: int) -> list[list[dict | None]]:
    """Lay the answer key out in columns read downwards.

    The instructor's own key is a grid of five pairs across and ten rows
    down, read down each column rather than across the page, so that is how
    this is built: column one holds items 1-10, column two 11-20, and so on.
    """
    columns = [
        rows[start:start + per_column]
        for start in range(0, len(rows), per_column)
    ]
    grid = []
    for line in range(per_column):
        grid.append([
            column[line] if line < len(column) else None
            for column in columns
        ])
    return grid


def _bounded(value, fallback: int, low: int, high: int) -> int:
    """Keep a submitted number inside the range the setting makes sense in."""
    if value is None:
        value = fallback
    return max(low, min(high, int(value)))


def _require_exam(exam_id: int) -> Exam:
    exam = Exam.query.get(exam_id)
    if exam is None:
        abort(404)
    require_course(exam.course_id)
    return exam


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #

@bp.route("/<int:exam_id>")
@staff_required
def edit(exam_id: int):
    exam = _require_exam(exam_id)
    course = exam.course

    banks = QuestionBank.query.filter_by(course_id=course.id).order_by(QuestionBank.name).all()
    rubrics = (
        Rubric.query.filter(
            or_(Rubric.course_id == course.id, Rubric.is_template.is_(True))
        )
        .order_by(Rubric.is_template, Rubric.name)
        .all()
    )

    return render_template(
        "exams/edit.html",
        exam=exam,
        course=course,
        question_types=Question.TYPES,
        difficulties=Question.DIFFICULTIES,
        banks=banks,
        rubrics=rubrics,
        categories=Exam.CATEGORIES,
        exit_modes=Exam.EXIT_MODES,
        sections=list(course.sections),
        gradebook_items=_linkable_items(course.id),
    )


@bp.route("/<int:exam_id>/settings", methods=["POST"])
@staff_required
def update_settings(exam_id: int):
    exam = _require_exam(exam_id)
    form = request.form

    if exam.is_open and form.get("duration_minutes"):
        new_duration = form.get("duration_minutes", type=int) or exam.duration_minutes
        if new_duration != exam.duration_minutes and exam.opened_at:
            # Shift the shared deadline rather than restarting the clock.
            exam.closes_at = exam.opened_at + timedelta(minutes=new_duration)
            flash("The shared deadline has been adjusted for everyone.", "info")
        exam.duration_minutes = max(1, new_duration)
    else:
        exam.duration_minutes = max(1, form.get("duration_minutes", type=int) or 60)

    exam.title = (form.get("title") or exam.title).strip()
    exam.category = form.get("category") or exam.category
    exam.topic = (form.get("topic") or "").strip()
    exam.instructions = (form.get("instructions") or "").strip()
    exam.section_id = form.get("section_id", type=int) or None
    exam.access_code = (form.get("access_code") or "").strip() or exam.access_code
    exam.shuffle_questions = bool(form.get("shuffle_questions"))
    exam.shuffle_options = bool(form.get("shuffle_options"))
    exam.exit_mode = form.get("exit_mode") or EXIT_STRICT
    exam.exit_grace_seconds = form.get("exit_grace_seconds", type=int) or 180
    exam.lock_grace_seconds = _bounded(
        form.get("lock_grace_seconds", type=int), 90, 5, 600
    )
    exam.exit_allowance = _bounded(form.get("exit_allowance", type=int), 1, 0, 10)
    exam.idle_seconds = _bounded(form.get("idle_seconds", type=int), 0, 0, 3600)
    exam.block_paste = bool(form.get("block_paste"))
    exam.watermark = bool(form.get("watermark"))
    exam.hide_when_unfocused = bool(form.get("hide_when_unfocused"))
    exam.allow_calculator = bool(form.get("allow_calculator"))
    exam.allow_backtrack = bool(form.get("allow_backtrack"))
    exam.one_question_per_page = bool(form.get("one_question_per_page"))
    exam.show_score = bool(form.get("show_score"))
    exam.show_answers = bool(form.get("show_answers"))
    exam.bank_draw_count = form.get("bank_draw_count", type=int) or 0
    exam.link_item_id = form.get("link_item_id", type=int) or None

    log_action(current_user, "exam_settings", "exam", exam.id)
    db.session.commit()
    flash("Exam settings saved.", "success")
    return redirect(url_for("exams.edit", exam_id=exam.id))


@bp.route("/<int:exam_id>/delete", methods=["POST"])
@staff_required
def delete(exam_id: int):
    exam = _require_exam(exam_id)
    attempts = Attempt.query.filter_by(exam_id=exam.id).count()
    if attempts and request.form.get("confirm") != "DELETE":
        flash(
            f"This exam has {attempts} attempt(s). Type DELETE to confirm — "
            f"the attempts and scores go with it.",
            "error",
        )
        return redirect(url_for("exams.edit", exam_id=exam.id))

    course_id = exam.course_id
    label = exam.title
    Attempt.query.filter_by(exam_id=exam.id).delete(synchronize_session=False)
    db.session.delete(exam)
    log_action(current_user, "exam_deleted", "exam", exam_id, label)
    db.session.commit()
    flash(f"Deleted “{label}”.", "success")
    return redirect(url_for("courses.detail", course_id=course_id))


@bp.route("/<int:exam_id>/duplicate", methods=["POST"])
@staff_required
def duplicate(exam_id: int):
    """Copy an exam, optionally into a different subject or term."""
    exam = _require_exam(exam_id)
    target_id = request.form.get("target_course_id", type=int) or exam.course_id
    target = require_course(target_id)

    clone = Exam(
        course_id=target.id,
        section_id=None,
        title=(request.form.get("title") or f"{exam.title} (copy)").strip(),
        category=exam.category,
        topic=exam.topic,
        instructions=exam.instructions,
        duration_minutes=exam.duration_minutes,
        access_code=secrets.token_hex(3).upper(),
        shuffle_questions=exam.shuffle_questions,
        shuffle_options=exam.shuffle_options,
        exit_mode=exam.exit_mode,
        exit_grace_seconds=exam.exit_grace_seconds,
        lock_grace_seconds=exam.lock_grace_seconds,
        exit_allowance=exam.exit_allowance,
        idle_seconds=exam.idle_seconds,
        block_paste=exam.block_paste,
        watermark=exam.watermark,
        hide_when_unfocused=exam.hide_when_unfocused,
        allow_calculator=exam.allow_calculator,
        allow_backtrack=exam.allow_backtrack,
        one_question_per_page=exam.one_question_per_page,
        show_score=exam.show_score,
        show_answers=exam.show_answers,
        bank_draw_count=exam.bank_draw_count,
        school_year=target.school_year,
        semester=target.semester,
        created_by_id=current_user.id,
    )
    db.session.add(clone)
    db.session.flush()

    for question in exam.questions:
        copy = _clone_question(question, exam_id=clone.id)
        db.session.add(copy)
        db.session.flush()
        for part in question.parts:
            db.session.add(
                SubQuestion(
                    question_id=copy.id,
                    label=part.label,
                    text=part.text,
                    answer_type=part.answer_type,
                    answer=part.answer,
                    options_raw=part.options_raw,
                    points=part.points,
                    tolerance_percent=part.tolerance_percent,
                    unit=part.unit,
                    order_index=part.order_index,
                )
            )

    log_action(current_user, "exam_duplicated", "exam", clone.id, f"from {exam.id}")
    db.session.commit()
    flash(f"Copied to {target.code} as “{clone.title}”.", "success")
    return redirect(url_for("exams.edit", exam_id=clone.id))


def _clone_question(question: Question, exam_id=None, bank_id=None) -> Question:
    return Question(
        exam_id=exam_id,
        bank_id=bank_id,
        qtype=question.qtype,
        text=question.text,
        options_raw=question.options_raw,
        answer=question.answer,
        points=question.points,
        tolerance_percent=question.tolerance_percent,
        unit=question.unit,
        figure_path=question.figure_path,
        figure_caption=question.figure_caption,
        rubric_id=question.rubric_id,
        explanation=question.explanation,
        topic=question.topic,
        difficulty=question.difficulty,
        order_index=question.order_index,
    )


# --------------------------------------------------------------------------- #
# Opening and closing -- one shared clock for the whole class
# --------------------------------------------------------------------------- #

@bp.route("/<int:exam_id>/open", methods=["POST"])
@staff_required
def open_exam(exam_id: int):
    exam = _require_exam(exam_id)
    if not exam.questions:
        flash("Add at least one question before opening the exam.", "error")
        return redirect(url_for("exams.edit", exam_id=exam.id))

    exam.is_open = True
    exam.opened_at = utcnow()
    exam.closes_at = exam.opened_at + timedelta(minutes=exam.duration_minutes)
    exam.closed_at = None
    log_action(current_user, "exam_opened", "exam", exam.id)
    db.session.commit()

    flash(
        f"“{exam.title}” is open. The clock is running for everyone and the "
        f"exam closes at {exam.closes_at.strftime('%I:%M %p')} server time. "
        f"The access code is {exam.access_code}.",
        "success",
    )
    return redirect(url_for("exams.monitor", exam_id=exam.id))


@bp.route("/<int:exam_id>/close", methods=["POST"])
@staff_required
def close_exam(exam_id: int):
    """Close for everyone and submit whatever is on screen."""
    exam = _require_exam(exam_id)
    submitted = _close_and_submit(exam, reason="closed_by_instructor")
    log_action(current_user, "exam_closed", "exam", exam.id, f"submitted={submitted}")
    db.session.commit()
    flash(
        f"“{exam.title}” is closed."
        + (f" {submitted} attempt(s) in progress were submitted." if submitted else ""),
        "success",
    )
    return redirect(url_for("exams.results", exam_id=exam.id))


def _close_and_submit(exam: Exam, reason: str) -> int:
    exam.is_open = False
    exam.closed_at = utcnow()

    questions = list(exam.questions)
    count = 0
    for attempt in Attempt.query.filter_by(
        exam_id=exam.id, status=ATTEMPT_IN_PROGRESS
    ).all():
        attempt.status = ATTEMPT_SUBMITTED
        attempt.submitted_at = utcnow()
        attempt.auto_submitted = True
        attempt.submit_reason = reason
        mark_attempt(attempt, questions)
        count += 1

    if count:
        _sync_to_gradebook(exam)
    return count


def _sync_to_gradebook(exam: Exam) -> None:
    """Push exam scores into the linked gradebook item, keeping overrides."""
    if not exam.link_item_id:
        return
    item = GradeItem.query.get(exam.link_item_id)
    if item is None:
        return

    for attempt in Attempt.query.filter_by(
        exam_id=exam.id, status=ATTEMPT_SUBMITTED
    ).all():
        row = Score.query.filter_by(
            item_id=item.id, student_id=attempt.student_id
        ).first()
        if row is None:
            row = Score(item_id=item.id, student_id=attempt.student_id)
            db.session.add(row)
        if row.is_override:
            continue                       # a manual figure always wins
        total = attempt.total_points or 0.0
        if total > 0 and item.max_points:
            row.points = round(
                attempt.effective_score / total * item.max_points, 2
            )
        else:
            row.points = attempt.effective_score


# --------------------------------------------------------------------------- #
# Questions
# --------------------------------------------------------------------------- #

@bp.route("/<int:exam_id>/questions", methods=["POST"])
@staff_required
def question_add(exam_id: int):
    exam = _require_exam(exam_id)
    question, problems = _build_question(request, exam_id=exam.id)
    if question is None:
        for problem in problems:
            flash(problem, "error")
        return redirect(url_for("exams.edit", exam_id=exam.id) + "#add-question")

    last = (
        db.session.query(func.max(Question.order_index))
        .filter(Question.exam_id == exam.id)
        .scalar()
        or 0
    )
    question.order_index = last + 1
    db.session.add(question)
    db.session.flush()
    _attach_parts(question, request, problems)

    log_action(current_user, "question_added", "exam", exam.id, question.qtype)
    db.session.commit()

    if problems:
        flash(
            "Question saved, but some sub-items need a look: " + "; ".join(problems[:5]),
            "warning",
        )
    else:
        flash("Question added.", "success")

    if request.form.get("save_to_bank"):
        bank_id = request.form.get("bank_id", type=int)
        if bank_id:
            copy = _clone_question(question, bank_id=bank_id)
            db.session.add(copy)
            db.session.commit()
            flash("A copy was saved to the question bank.", "info")

    return redirect(url_for("exams.edit", exam_id=exam.id) + "#add-question")


@bp.route("/questions/<int:question_id>", methods=["POST"])
@staff_required
def question_edit(question_id: int):
    question = Question.query.get_or_404(question_id)
    if question.exam_id:
        exam = _require_exam(question.exam_id)
        back = url_for("exams.edit", exam_id=exam.id)
    else:
        bank = QuestionBank.query.get_or_404(question.bank_id)
        require_course(bank.course_id)
        back = url_for("exams.bank_detail", bank_id=bank.id)

    action = request.form.get("action")

    if action == "delete":
        db.session.delete(question)
        db.session.commit()
        flash("Question removed.", "success")
        return redirect(back)

    if action in ("up", "down"):
        _reorder(question, action)
        return redirect(back)

    updated, problems = _build_question(request, exam_id=question.exam_id,
                                        bank_id=question.bank_id, existing=question)
    if updated is None:
        for problem in problems:
            flash(problem, "error")
        return redirect(back)

    SubQuestion.query.filter_by(question_id=question.id).delete(synchronize_session=False)
    db.session.flush()
    _attach_parts(question, request, problems)

    log_action(current_user, "question_edited", "question", question.id)
    db.session.commit()

    if problems:
        flash("Saved, with notes: " + "; ".join(problems[:5]), "warning")
    else:
        flash("Question saved.", "success")
    return redirect(back)


def _reorder(question: Question, direction: str) -> None:
    siblings = (
        Question.query.filter_by(exam_id=question.exam_id)
        .order_by(Question.order_index, Question.id)
        .all()
    )
    index = next((i for i, q in enumerate(siblings) if q.id == question.id), None)
    if index is None:
        return
    swap_with = index - 1 if direction == "up" else index + 1
    if swap_with < 0 or swap_with >= len(siblings):
        return
    other = siblings[swap_with]
    question.order_index, other.order_index = (
        other.order_index or swap_with + 1,
        question.order_index or index + 1,
    )
    db.session.commit()


def _build_question(req, exam_id=None, bank_id=None, existing: Question | None = None):
    """Validate and populate a question from the form."""
    form = req.form
    problems: list[str] = []

    qtype = form.get("qtype") or Q_MCQ
    if qtype not in dict(Question.TYPES):
        return None, ["That question type is not recognised."]

    def answer_for(kind: str) -> str:
        """Read the answer field belonging to this question type.

        The editor shows the fields for every type at once so switching type
        does not discard typed work, which means several of them are called
        "answer".  Each is given its own name, and the right one is picked
        here -- so the form is correct even with scripting switched off.
        """
        specific = {
            Q_MCQ: "answer_mcq",
            Q_TRUEFALSE: "answer_tf",
            Q_NUMERIC: "answer_num",
            Q_SHORT: "answer_short",
            Q_ESSAY: "answer_essay",
        }.get(kind)
        if specific and form.get(specific) is not None:
            return form.get(specific) or ""
        return form.get("answer") or ""

    text = (form.get("text") or "").strip()
    if not text:
        return None, ["The question text cannot be empty."]

    points = form.get("points", type=float)
    if points is None or points <= 0:
        points = 1.0

    question = existing or Question(exam_id=exam_id, bank_id=bank_id)
    question.qtype = qtype
    question.text = text
    question.points = points
    question.explanation = (form.get("explanation") or "").strip()
    question.topic = (form.get("topic") or "").strip()
    question.difficulty = form.get("difficulty") or "moderate"
    question.unit = (form.get("unit") or "").strip()
    question.tolerance_percent = form.get("tolerance_percent", type=float) or 0.0
    question.figure_caption = (form.get("figure_caption") or "").strip()

    rubric_id = form.get("rubric_id", type=int) or None
    question.rubric_id = rubric_id

    # ---- type-specific ---------------------------------------------------- #
    if qtype in (Q_MCQ,):
        options = [
            (form.get(f"option_{i}") or "").strip()
            for i in range(1, 11)
        ]
        options = [o for o in options if o]
        if len(options) < 2:
            return None, ["A multiple-choice question needs at least two options."]
        question.options = options

        answer = answer_for(Q_MCQ).strip()
        if answer.isdigit():
            index = int(answer) - 1
            if not 0 <= index < len(options):
                return None, ["The correct option number is outside the list."]
            answer = options[index]
        if answer not in options:
            return None, ["The correct answer must be one of the options."]
        question.answer = answer

    elif qtype == Q_TRUEFALSE:
        question.options = ["True", "False"]
        answer = answer_for(Q_TRUEFALSE).strip().title()
        if answer not in ("True", "False"):
            return None, ["Choose True or False as the correct answer."]
        question.answer = answer

    elif qtype == Q_NUMERIC:
        from ..marking import parse_number

        raw = answer_for(Q_NUMERIC).strip()
        if parse_number(raw) is None:
            return None, ["Give a numerical answer, for example 101.325."]
        question.answer = raw
        question.options = []

    elif qtype == Q_SHORT:
        answer = answer_for(Q_SHORT).strip()
        if not answer:
            return None, ["Give at least one accepted answer."]
        question.answer = answer
        question.options = []

    elif qtype == Q_ESSAY:
        question.answer = answer_for(Q_ESSAY).strip()          # guide answer
        question.options = []

    elif qtype == Q_MATCHING:
        pairs = []
        for i in range(1, 21):
            left = (form.get(f"pair_left_{i}") or "").strip()
            right = (form.get(f"pair_right_{i}") or "").strip()
            if left and right:
                pairs.append({"left": left, "right": right})
        bulk = (form.get("pairs_bulk") or "").strip()
        if bulk:
            for line_no, line in enumerate(bulk.splitlines(), start=1):
                if not line.strip():
                    continue
                if "=" in line:
                    left, right = line.split("=", 1)
                elif "|" in line:
                    left, right = line.split("|", 1)
                else:
                    problems.append(f"matching line {line_no}: use ‘left = right’")
                    continue
                if left.strip() and right.strip():
                    pairs.append({"left": left.strip(), "right": right.strip()})
        if len(pairs) < 2:
            return None, ["A matching question needs at least two pairs."]

        # The answer for pair i is right-hand item i; the delivery view
        # shuffles the right column for the student.
        for index, pair in enumerate(pairs):
            pair["answer"] = index
        question.pairs = pairs

    elif qtype == Q_MULTIPART:
        question.options = []
        question.answer = ""

    return question, problems


def _attach_parts(question: Question, req, problems: list[str]) -> None:
    """Build the lettered sub-items of a multi-part problem."""
    if question.qtype != Q_MULTIPART:
        return

    form = req.form
    parts: list[dict] = []

    # Structured rows from the form.
    for i in range(1, 21):
        text = (form.get(f"part_text_{i}") or "").strip()
        answer = (form.get(f"part_answer_{i}") or "").strip()
        if not text and not answer:
            continue
        parts.append(
            {
                "label": (form.get(f"part_label_{i}") or "").strip(),
                "text": text,
                "answer": answer,
                "answer_type": form.get(f"part_type_{i}") or Q_NUMERIC,
                "points": form.get(f"part_points_{i}", type=float) or 1.0,
                "tolerance": form.get(f"part_tolerance_{i}", type=float) or 0.0,
                "unit": (form.get(f"part_unit_{i}") or "").strip(),
            }
        )

    # Bulk paste, parsed tolerantly.
    bulk = (form.get("parts_bulk") or "").strip()
    if bulk:
        parts.extend(parse_parts_bulk(bulk, problems))

    if not parts:
        problems.append(
            "No sub-items were given, so the problem was saved with none. "
            "Add at least one."
        )

    for index, spec in enumerate(parts, start=1):
        label = spec["label"] or _letter(index)
        db.session.add(
            SubQuestion(
                question_id=question.id,
                label=label,
                text=spec["text"],
                answer=spec["answer"],
                answer_type=normalise_part_type(spec["answer_type"]),
                points=spec["points"],
                tolerance_percent=spec["tolerance"],
                unit=spec["unit"],
                order_index=index,
            )
        )


def _letter(index: int) -> str:
    letters = "abcdefghijklmnopqrstuvwxyz"
    return letters[index - 1] if index <= len(letters) else str(index)


# The parsing of sub-items lives in app/question_io.py so that the editor form
# and the CSV importer cannot drift apart: a line typed into the browser and
# the same line in a spreadsheet are read by exactly the same code.

# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #

@bp.route("/questions/<int:question_id>/figure", methods=["POST"])
@staff_required
def figure_upload(question_id: int):
    question = Question.query.get_or_404(question_id)
    if question.exam_id:
        _require_exam(question.exam_id)
    else:
        bank = QuestionBank.query.get_or_404(question.bank_id)
        require_course(bank.course_id)

    upload = request.files.get("figure")
    if upload is None or not upload.filename:
        flash("Choose an image first.", "error")
        return redirect(request.referrer or url_for("exams.index"))

    extension = os.path.splitext(upload.filename)[1].lower()
    if extension not in ALLOWED_FIGURES:
        flash(
            "Figures must be PNG, JPG, GIF, WEBP or SVG.",
            "error",
        )
        return redirect(request.referrer or url_for("exams.index"))

    name = secure_filename(f"q{question.id}-{secrets.token_hex(4)}{extension}")
    directory = current_app.config["UPLOAD_DIR"] / "figures"
    directory.mkdir(parents=True, exist_ok=True)
    upload.save(directory / name)

    # Replace any previous figure so the data directory does not fill up.
    if question.figure_path:
        old = directory / question.figure_path
        try:
            if old.is_file():
                old.unlink()
        except OSError:
            pass

    question.figure_path = name
    question.figure_caption = (request.form.get("figure_caption") or "").strip()
    db.session.commit()
    flash("Figure attached.", "success")
    return redirect(request.referrer or url_for("exams.index"))


@bp.route("/questions/<int:question_id>/figure/remove", methods=["POST"])
@staff_required
def figure_remove(question_id: int):
    question = Question.query.get_or_404(question_id)
    if question.exam_id:
        _require_exam(question.exam_id)
    if question.figure_path:
        target = current_app.config["UPLOAD_DIR"] / "figures" / question.figure_path
        try:
            if target.is_file():
                target.unlink()
        except OSError:
            pass
        question.figure_path = None
        question.figure_caption = ""
        db.session.commit()
    flash("Figure removed.", "success")
    return redirect(request.referrer or url_for("exams.index"))


@bp.route("/figure/<path:name>")
def figure(name: str):
    """Figures live outside the source tree, so they survive a redeploy."""
    directory = current_app.config["UPLOAD_DIR"] / "figures"
    safe = secure_filename(name)
    if not (directory / safe).is_file():
        abort(404)
    response = send_from_directory(directory, safe)
    response.headers["Cache-Control"] = "public, max-age=604800"
    return response


# --------------------------------------------------------------------------- #
# Question banks
# --------------------------------------------------------------------------- #

@bp.route("/banks", methods=["GET", "POST"])
@staff_required
def banks():
    courses = visible_courses().all()
    if request.method == "POST":
        course = require_course(request.form.get("course_id", type=int))
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Give the bank a name.", "error")
            return redirect(url_for("exams.banks"))
        bank = QuestionBank(
            course_id=course.id,
            name=name,
            description=(request.form.get("description") or "").strip(),
        )
        db.session.add(bank)
        db.session.commit()
        flash(f"Created the bank “{name}”.", "success")
        return redirect(url_for("exams.bank_detail", bank_id=bank.id))

    course_ids = [c.id for c in courses]
    listing = (
        QuestionBank.query.filter(QuestionBank.course_id.in_(course_ids or [-1]))
        .order_by(QuestionBank.name)
        .all()
    )
    return render_template("exams/banks.html", banks=listing, courses=courses)


@bp.route("/banks/<int:bank_id>")
@staff_required
def bank_detail(bank_id: int):
    bank = QuestionBank.query.get_or_404(bank_id)
    course = require_course(bank.course_id)
    questions = (
        Question.query.filter_by(bank_id=bank.id)
        .order_by(Question.topic, Question.id)
        .all()
    )
    topics = sorted({q.topic for q in questions if q.topic})
    rubrics = (
        Rubric.query.filter(
            or_(Rubric.course_id == course.id, Rubric.is_template.is_(True))
        ).all()
    )
    return render_template(
        "exams/bank_detail.html",
        bank=bank,
        course=course,
        questions=questions,
        topics=topics,
        question_types=Question.TYPES,
        difficulties=Question.DIFFICULTIES,
        rubrics=rubrics,
    )


@bp.route("/banks/<int:bank_id>/questions", methods=["POST"])
@staff_required
def bank_question_add(bank_id: int):
    bank = QuestionBank.query.get_or_404(bank_id)
    require_course(bank.course_id)

    question, problems = _build_question(request, bank_id=bank.id)
    if question is None:
        for problem in problems:
            flash(problem, "error")
        return redirect(url_for("exams.bank_detail", bank_id=bank.id))

    db.session.add(question)
    db.session.flush()
    _attach_parts(question, request, problems)
    db.session.commit()
    flash("Question added to the bank.", "success")
    return redirect(url_for("exams.bank_detail", bank_id=bank.id))


# --------------------------------------------------------------------------- #
# Bulk question loading, by spreadsheet
# --------------------------------------------------------------------------- #

def _question_from_spec(spec: dict, exam_id=None, bank_id=None) -> Question:
    """Turn one parsed CSV row into a question, with its sub-items attached."""
    question = Question(
        exam_id=exam_id,
        bank_id=bank_id,
        qtype=spec["qtype"],
        text=spec["text"],
        points=spec["points"],
        answer=spec["answer"],
        tolerance_percent=spec["tolerance_percent"],
        unit=spec["unit"],
        topic=spec["topic"],
        difficulty=spec["difficulty"],
        explanation=spec["explanation"],
    )
    if spec["qtype"] == Q_MATCHING:
        question.pairs = spec["options"]
    else:
        question.options = spec["options"]

    db.session.add(question)
    db.session.flush()

    for order, part in enumerate(spec["parts"], start=1):
        db.session.add(
            SubQuestion(
                question_id=question.id,
                label=part["label"] or _letter(order),
                text=part["text"],
                answer_type=part["answer_type"],
                answer=part["answer"],
                points=part["points"],
                tolerance_percent=part["tolerance"],
                unit=part["unit"],
                order_index=order,
            )
        )
    return question


def _csv_payload(req) -> tuple[str, str | None]:
    """The spreadsheet text, from either an upload or the paste box."""
    upload = req.files.get("csv_file")
    if upload and upload.filename:
        name = upload.filename.lower()
        if not name.endswith((".csv", ".txt", ".tsv")):
            return "", (
                "Save the sheet as CSV first — in Excel, File ▸ Save As ▸ "
                "“CSV UTF-8 (Comma delimited)”. An .xlsx file cannot be read "
                "directly."
            )
        raw = upload.read()
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return raw.decode(encoding), None
            except UnicodeDecodeError:
                continue
        return "", "That file's text could not be read. Save it as CSV UTF-8."
    return (req.form.get("csv_text") or ""), None


@bp.route("/banks/<int:bank_id>/import", methods=["POST"])
@staff_required
def bank_import(bank_id: int):
    """Load a sheet of questions into a bank, checking it first.

    Nothing is written until the instructor has seen what will be created.
    The first pass reports what it read and what it could not; only a second
    submission with ``confirm`` actually saves, and it saves all or nothing.
    """
    bank = QuestionBank.query.get_or_404(bank_id)
    course = require_course(bank.course_id)

    text, error = _csv_payload(request)
    if error:
        flash(error, "error")
        return redirect(url_for("exams.bank_detail", bank_id=bank.id))

    specs, problems = parse_questions_csv(text)

    default_topic = (request.form.get("default_topic") or "").strip()
    if default_topic:
        for spec in specs:
            if not spec["topic"]:
                spec["topic"] = default_topic

    if not specs:
        for problem in problems[:25]:
            flash(problem, "error")
        if len(problems) > 25:
            flash(f"…and {len(problems) - 25} more problems.", "error")
        flash("Nothing was imported.", "warning")
        return redirect(url_for("exams.bank_detail", bank_id=bank.id))

    if not request.form.get("confirm"):
        return render_template(
            "exams/bank_import_review.html",
            bank=bank,
            course=course,
            specs=specs,
            problems=problems,
            csv_text=text,
            default_topic=default_topic,
            question_types=dict(Question.TYPES),
        )

    created = 0
    try:
        for spec in specs:
            _question_from_spec(spec, bank_id=bank.id)
            created += 1
        log_action(
            current_user,
            "bank.import",
            entity="question_bank",
            entity_id=bank.id,
            detail=f"{created} questions imported into “{bank.name}”",
        )
        db.session.commit()
    except Exception:  # noqa: BLE001 - a half-loaded bank is worse than none
        db.session.rollback()
        current_app.logger.exception("question bank import failed")
        flash(
            "The import failed part-way and nothing was saved, so the bank is "
            "exactly as it was. Check the sheet and try again.",
            "error",
        )
        return redirect(url_for("exams.bank_detail", bank_id=bank.id))

    flash(f"Added {created} question{'s' if created != 1 else ''} to the bank.",
          "success")
    if problems:
        flash(
            f"{len(problems)} row{'s' if len(problems) != 1 else ''} were "
            f"skipped or adjusted — see the warnings above the sheet.",
            "warning",
        )
    return redirect(url_for("exams.bank_detail", bank_id=bank.id))


@bp.route("/banks/<int:bank_id>/export.csv")
@staff_required
def bank_export(bank_id: int):
    """The bank as a spreadsheet, in the same shape the importer reads."""
    bank = QuestionBank.query.get_or_404(bank_id)
    require_course(bank.course_id)
    questions = (
        Question.query.filter_by(bank_id=bank.id)
        .order_by(Question.topic, Question.id)
        .all()
    )
    body = questions_to_csv(questions)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", bank.name).strip("-").lower() or "bank"
    return current_app.response_class(
        body,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{slug}-questions.csv"',
            "Cache-Control": "no-store",
        },
    )


@bp.route("/questions/template.csv")
@staff_required
def question_template():
    """A worked example of every question type, to edit and send back."""
    return current_app.response_class(
        template_csv(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="question-template.csv"',
            "Cache-Control": "no-store",
        },
    )


@bp.route("/<int:exam_id>/import", methods=["POST"])
@staff_required
def exam_import(exam_id: int):
    """Load a sheet of questions straight onto an exam."""
    exam = _require_exam(exam_id)

    text, error = _csv_payload(request)
    if error:
        flash(error, "error")
        return redirect(url_for("exams.edit", exam_id=exam.id))

    specs, problems = parse_questions_csv(text)
    if not specs:
        for problem in problems[:25]:
            flash(problem, "error")
        flash("Nothing was imported.", "warning")
        return redirect(url_for("exams.edit", exam_id=exam.id))

    last = (
        db.session.query(func.max(Question.order_index))
        .filter(Question.exam_id == exam.id)
        .scalar()
        or 0
    )
    created = 0
    try:
        for offset, spec in enumerate(specs, start=1):
            question = _question_from_spec(spec, exam_id=exam.id)
            question.order_index = last + offset
            created += 1
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception("exam question import failed")
        flash("The import failed and nothing was saved.", "error")
        return redirect(url_for("exams.edit", exam_id=exam.id))

    flash(f"Added {created} question{'s' if created != 1 else ''} to the exam.",
          "success")
    for problem in problems[:10]:
        flash(problem, "warning")
    return redirect(url_for("exams.edit", exam_id=exam.id))


@bp.route("/<int:exam_id>/questions.csv")
@staff_required
def exam_export(exam_id: int):
    exam = _require_exam(exam_id)
    questions = (
        Question.query.filter_by(exam_id=exam.id)
        .order_by(Question.order_index, Question.id)
        .all()
    )
    slug = re.sub(r"[^A-Za-z0-9]+", "-", exam.title).strip("-").lower() or "exam"
    return current_app.response_class(
        questions_to_csv(questions),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{slug}-questions.csv"',
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------- #
# Seeing it as a student does
# --------------------------------------------------------------------------- #

@bp.route("/<int:exam_id>/preview")
@staff_required
def preview(exam_id: int):
    """The exam exactly as a student sees it, without creating an attempt.

    It renders through the same template partial the delivery page uses, so
    there is nothing to keep in step: if the preview looks right, the exam
    looks right. Nothing is written — no attempt, no answers — so this can be
    opened on an exam that is already running.
    """
    exam = _require_exam(exam_id)
    questions = list(exam.questions)

    seed = request.args.get("seed", type=int) or 1
    if request.args.get("order") == "shuffled" and exam.shuffle_questions:
        questions = list(questions)
        random.Random(seed * 7919).shuffle(questions)

    from .taking import _layout                       # the student's own view

    return render_template(
        "exams/preview.html",
        exam=exam,
        course=exam.course,
        questions=questions,
        layouts={q.id: _layout(q, seed, exam) for q in questions},
        saved={},
        preview=True,
        seed=seed,
        total_points=sum(q.effective_points for q in questions),
    )


# --------------------------------------------------------------------------- #
# The paper version
# --------------------------------------------------------------------------- #

@bp.route("/<int:exam_id>/paper")
@staff_required
def paper(exam_id: int):
    """The exam as a printable paper, with an optional answer key.

    Power and signal are not guaranteed, and an examination that cannot be
    sat is worse than one sat on paper. This prints the same questions, in
    the same order, so the paper and the screen cannot disagree.
    """
    exam = _require_exam(exam_id)
    questions = (
        Question.query.filter_by(exam_id=exam.id)
        .order_by(Question.order_index, Question.id)
        .all()
    )

    show_key = request.args.get("key", "1") != "0"
    shuffle = request.args.get("shuffle") == "1"
    sets = max(1, min(request.args.get("sets", type=int) or 1, 4))
    answer_sheet = request.args.get("sheet") == "1"
    spacing = request.args.get("space", "normal")

    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    papers = []
    for index in range(sets):
        # Seeded on the exam and the set, so reprinting Set B gives exactly
        # the same Set B. An unseeded shuffle would hand out papers whose
        # printed key no longer matches them.
        rng = random.Random(f"{exam.id}-{index}")

        ordered = list(questions)
        if shuffle and index > 0:
            rng.shuffle(ordered)

        rows = []
        slots = []            # the answer-sheet boxes, numbered as printed
        for number, question in enumerate(ordered, start=1):
            row = {"number": number, "question": question, "key": "", "column_b": []}

            if question.qtype in (Q_MCQ, Q_TRUEFALSE):
                options = list(question.options or [])
                if question.qtype == Q_MCQ and exam.shuffle_options and shuffle:
                    rng.shuffle(options)
                row["options"] = options
                # How many across. The Word template's tab stops leave only
                # about 1.2in for the fourth option, so anything longer than a
                # word or two wrapped down the page. Four equal columns fit
                # the same arrangement without wrapping; longer options drop
                # to two across and the longest to one per line.
                longest = max((len(str(o)) for o in options), default=0)
                if longest <= 22 and len(options) <= 4:
                    row["columns"] = 4
                elif longest <= 48:
                    row["columns"] = 2
                else:
                    row["columns"] = 1
                for position, option in enumerate(options):
                    if option == question.answer:
                        row["key"] = f"{letters[position]} — {option}"
                        row["key_letter"] = letters[position]
                        break
                slots.append(f"{number}")

            elif question.qtype == Q_MATCHING:
                pairs = list(question.pairs or [])
                order = list(range(len(pairs)))
                if shuffle:
                    rng.shuffle(order)
                row["column_b"] = [pairs[i] for i in order]
                # Where each left-hand item ended up in the printed column B.
                where = {original: printed for printed, original in enumerate(order)}
                row["key"] = ", ".join(
                    f"{i + 1}={letters[where[i]]}" for i in range(len(pairs))
                )
                slots.extend(f"{number}.{i + 1}" for i in range(len(pairs)))

            elif question.qtype == Q_MULTIPART:
                row["key"] = "; ".join(
                    f"{p.label}) {p.answer}{(' ' + p.unit) if p.unit else ''}"
                    for p in question.parts
                )
                slots.extend(
                    f"{number}{p.label or letters[i].lower()}"
                    for i, p in enumerate(question.parts)
                )

            elif question.qtype == Q_ESSAY:
                row["key"] = (
                    f"marked by hand — guide: {question.answer}"
                    if question.answer else "marked by hand"
                )

            else:
                tail = f" {question.unit}" if question.unit else ""
                if question.tolerance_percent:
                    tail += f" (±{question.tolerance_percent:g}%)"
                row["key"] = f"{question.answer}{tail}"
                slots.append(f"{number}")

            rows.append(row)

        # Items are grouped into parts by topic, the way a paper is written:
        # "Part I — Stresses in Beams". A paper whose questions carry no topic
        # simply has no part headings, which is correct rather than a blank
        # one.
        parts = []
        for row in rows:
            topic = (row["question"].topic or "").strip()
            if parts and parts[-1]["topic"] == topic:
                parts[-1]["rows"].append(row)
            else:
                parts.append({"topic": topic, "rows": [row]})
        if len(parts) == 1 and not parts[0]["topic"]:
            parts[0]["numeral"] = ""
        else:
            for position, part in enumerate(parts, start=1):
                part["numeral"] = _roman(position)

        papers.append({
            "label": letters[index] if sets > 1 else "",
            "rows": rows,
            "parts": parts,
            "slots": slots,
            # The answer key is a five-column grid read down each column, as
            # on the instructor's own paper.
            "key_rows": _key_grid(rows, 10),
        })

    return render_template(
        "exams/paper.html",
        exam=exam,
        course=exam.course,
        papers=papers,
        show_key=show_key,
        shuffle=shuffle,
        sets=sets,
        answer_sheet=answer_sheet,
        spacing=spacing,
        total_points=sum(q.effective_points for q in questions),
        definition_items=sum(1 for q in questions if (q.effective_points or 0) <= 1),
        problem_items=sum(1 for q in questions if (q.effective_points or 0) > 1),
        checked_by=(
            request.args["checked_by"] if "checked_by" in request.args
            else Setting.get("program_chair", "")
        ).strip(),
        checked_title=(request.args.get("checked_title")
                       or Setting.get("program_chair_title", "")).strip()
        or "Program Chair",
        prepared_by=(request.args.get("prepared_by") or "").strip()
        or (exam.course.owner.name if exam.course.owner else ""),
        prepared_title=(request.args.get("prepared_title") or "Faculty").strip(),
        form_code=(
            request.args["form_code"] if "form_code" in request.args
            else Setting.get("exam_form_code", "")
        ).strip(),
        directions=(request.args.get("directions") or "").strip(),
    )


@bp.route("/<int:exam_id>/pull-from-bank", methods=["POST"])
@staff_required
def pull_from_bank(exam_id: int):
    """Copy questions from a bank into this exam, optionally at random."""
    exam = _require_exam(exam_id)
    bank = QuestionBank.query.get_or_404(request.form.get("bank_id", type=int))
    require_course(bank.course_id)

    pool = Question.query.filter_by(bank_id=bank.id)
    topic = (request.form.get("topic") or "").strip()
    if topic:
        pool = pool.filter(Question.topic == topic)
    difficulty = (request.form.get("difficulty") or "").strip()
    if difficulty:
        pool = pool.filter(Question.difficulty == difficulty)

    available = pool.all()
    if not available:
        flash("No questions in that bank match those filters.", "warning")
        return redirect(url_for("exams.edit", exam_id=exam.id))

    count = request.form.get("count", type=int) or len(available)
    count = max(1, min(count, len(available)))
    chosen = random.sample(available, count) if count < len(available) else available

    last = (
        db.session.query(func.max(Question.order_index))
        .filter(Question.exam_id == exam.id)
        .scalar()
        or 0
    )
    for offset, source in enumerate(chosen, start=1):
        copy = _clone_question(source, exam_id=exam.id)
        copy.order_index = last + offset
        db.session.add(copy)
        db.session.flush()
        for part in source.parts:
            db.session.add(
                SubQuestion(
                    question_id=copy.id,
                    label=part.label,
                    text=part.text,
                    answer_type=part.answer_type,
                    answer=part.answer,
                    options_raw=part.options_raw,
                    points=part.points,
                    tolerance_percent=part.tolerance_percent,
                    unit=part.unit,
                    order_index=part.order_index,
                )
            )

    log_action(current_user, "bank_pull", "exam", exam.id, f"{count} from bank {bank.id}")
    db.session.commit()
    flash(f"Added {count} question(s) from “{bank.name}”.", "success")
    return redirect(url_for("exams.edit", exam_id=exam.id))


# --------------------------------------------------------------------------- #
# Monitoring, results, item analysis
# --------------------------------------------------------------------------- #

@bp.route("/<int:exam_id>/monitor")
@staff_required
def monitor(exam_id: int):
    exam = _require_exam(exam_id)
    roster = exam.course.roster(exam.section_id)
    attempts = {
        a.student_id: a for a in Attempt.query.filter_by(exam_id=exam.id).all()
    }
    rows = [{"student": s, "attempt": attempts.get(s.id)} for s in roster]
    return render_template(
        "exams/monitor.html",
        exam=exam,
        rows=rows,
        seconds_remaining=exam.seconds_remaining,
        autosave_seconds=Setting.get_bounded("exam_autosave_seconds"),
    )


@bp.route("/<int:exam_id>/results")
@staff_required
def results(exam_id: int):
    exam = _require_exam(exam_id)
    attempts = (
        Attempt.query.filter_by(exam_id=exam.id)
        .order_by(Attempt.score.desc())
        .all()
    )
    roster = exam.course.roster(exam.section_id)
    taken = {a.student_id for a in attempts}
    missing = [s for s in roster if s.id not in taken]

    scores = [a.percent for a in attempts if a.is_submitted]
    stats = {}
    if scores:
        scores_sorted = sorted(scores)
        middle = len(scores_sorted) // 2
        stats = {
            "count": len(scores),
            "mean": round(sum(scores) / len(scores), 2),
            "median": round(
                scores_sorted[middle]
                if len(scores_sorted) % 2
                else (scores_sorted[middle - 1] + scores_sorted[middle]) / 2,
                2,
            ),
            "high": max(scores),
            "low": min(scores),
            "passed": sum(1 for s in scores if s >= 60),
        }

    return render_template(
        "exams/results.html", exam=exam, attempts=attempts, missing=missing, stats=stats
    )


@bp.route("/<int:exam_id>/analysis")
@staff_required
def analysis(exam_id: int):
    """Item analysis: difficulty and discrimination per question.

    Difficulty index is the proportion who answered correctly.  Discrimination
    is the upper-third proportion minus the lower-third proportion, the
    classic rule-of-thumb teachers are taught: above 0.30 is a good
    discriminator, near zero or negative means the item is not separating
    students who know the material from those who do not.
    """
    exam = _require_exam(exam_id)
    attempts = [
        a
        for a in Attempt.query.filter_by(exam_id=exam.id, status=ATTEMPT_SUBMITTED).all()
    ]
    attempts.sort(key=lambda a: a.percent, reverse=True)

    group_size = max(1, len(attempts) // 3)
    upper = {a.id for a in attempts[:group_size]} if len(attempts) >= 6 else set()
    lower = {a.id for a in attempts[-group_size:]} if len(attempts) >= 6 else set()

    by_question: dict[int, list] = {}
    for attempt in attempts:
        for answer in attempt.answers:
            if answer.sub_question_id:
                continue
            by_question.setdefault(answer.question_id, []).append((attempt.id, answer))

    rows = []
    for question in exam.questions:
        records = by_question.get(question.id, [])
        answered = len(records)
        if answered == 0:
            rows.append(
                {"question": question, "answered": 0, "difficulty": None,
                 "discrimination": None, "verdict": "No responses yet",
                 "distractors": {}}
            )
            continue

        def _credit(answer) -> float:
            if answer.max_points:
                return (answer.points_awarded or 0.0) / answer.max_points
            return 1.0 if answer.is_correct else 0.0

        difficulty = sum(_credit(a) for _, a in records) / answered

        discrimination = None
        if upper and lower:
            upper_scores = [_credit(a) for aid, a in records if aid in upper]
            lower_scores = [_credit(a) for aid, a in records if aid in lower]
            if upper_scores and lower_scores:
                discrimination = (
                    sum(upper_scores) / len(upper_scores)
                    - sum(lower_scores) / len(lower_scores)
                )

        if difficulty >= 0.90:
            verdict = "Very easy — nearly everyone got it"
        elif difficulty <= 0.25:
            verdict = "Very hard — check the wording and the key"
        elif discrimination is not None and discrimination < 0:
            verdict = "Negative discrimination — the key may be wrong"
        elif discrimination is not None and discrimination < 0.2:
            verdict = "Weak discriminator"
        else:
            verdict = "Working well"

        distractors: dict[str, int] = {}
        if question.qtype in (Q_MCQ, Q_TRUEFALSE):
            for _, answer in records:
                key = (answer.response or "(blank)")[:80]
                distractors[key] = distractors.get(key, 0) + 1

        rows.append(
            {
                "question": question,
                "answered": answered,
                "difficulty": round(difficulty, 3),
                "discrimination": round(discrimination, 3) if discrimination is not None else None,
                "verdict": verdict,
                "distractors": dict(
                    sorted(distractors.items(), key=lambda kv: kv[1], reverse=True)
                ),
            }
        )

    return render_template(
        "exams/analysis.html",
        exam=exam,
        rows=rows,
        sample=len(attempts),
        grouped=bool(upper and lower),
    )


@bp.route("/<int:exam_id>/rescore", methods=["POST"])
@staff_required
def rescore(exam_id: int):
    exam = _require_exam(exam_id)
    count = rescore_exam(exam)
    _sync_to_gradebook(exam)
    log_action(current_user, "exam_rescored", "exam", exam.id, f"{count} attempts")
    db.session.commit()
    flash(
        f"Re-marked {count} attempt(s) against the current answer key. "
        f"Instructor overrides were kept.",
        "success",
    )
    return redirect(url_for("exams.results", exam_id=exam.id))


@bp.route("/<int:exam_id>/sync", methods=["POST"])
@staff_required
def sync(exam_id: int):
    exam = _require_exam(exam_id)
    if not exam.link_item_id:
        flash("Link this exam to a gradebook activity first.", "error")
        return redirect(url_for("exams.results", exam_id=exam.id))
    _sync_to_gradebook(exam)
    db.session.commit()
    flash("Scores pushed to the gradebook.", "success")
    return redirect(url_for("exams.results", exam_id=exam.id))


# --------------------------------------------------------------------------- #
# Attempt management
# --------------------------------------------------------------------------- #

@bp.route("/attempts/<int:attempt_id>")
@staff_required
def attempt_detail(attempt_id: int):
    attempt = Attempt.query.get_or_404(attempt_id)
    exam = _require_exam(attempt.exam_id)
    answers = {}
    for answer in attempt.answers:
        answers.setdefault(answer.question_id, []).append(answer)
    return render_template(
        "exams/attempt.html", attempt=attempt, exam=exam, answers=answers
    )


# --------------------------------------------------------------------------- #
# The marked paper, for printing and handing back
# --------------------------------------------------------------------------- #

def _marked_paper(attempt: Attempt) -> list[dict]:
    """One student's paper, question by question, ready to print.

    Assembled here rather than in the template because the single-student
    report and the whole-class run must show exactly the same thing: a report
    that disagrees with itself depending on how it was printed is worse than
    no report.

    The questions come back in the order the *student* saw them, since a
    shuffled paper handed back in the instructor's order is confusing to check
    against.
    """
    by_question: dict[int, list] = {}
    for answer in attempt.answers:
        by_question.setdefault(answer.question_id, []).append(answer)

    questions = list(attempt.exam.questions)
    order = attempt.question_order
    if order:
        indexed = {q.id: q for q in questions}
        questions = [indexed[qid] for qid in order if qid in indexed]
        questions += [q for q in indexed.values() if q.id not in set(order)]

    rows = []
    for number, question in enumerate(questions, start=1):
        answers = sorted(
            by_question.get(question.id, []),
            key=lambda a: (a.sub_question.order_index if a.sub_question else 0),
        )
        earned = sum((a.points_awarded or 0.0) for a in answers)
        possible = sum((a.max_points or 0.0) for a in answers)
        if not possible:
            possible = question.effective_points

        rubric_score = None
        if question.rubric is not None and answers:
            rubric_score = (
                RubricScore.query.filter_by(answer_id=answers[0].id).first()
            )

        rows.append({
            "number": number,
            "question": question,
            "answers": answers,
            "answer": answers[0] if answers else None,
            "earned": earned,
            "possible": possible,
            "attempted": any((a.response or "").strip() for a in answers),
            "rubric_score": rubric_score,
        })
    return rows


@bp.route("/attempts/<int:attempt_id>/report")
@staff_required
def attempt_report(attempt_id: int):
    """A printable record of one student's paper, with the marking shown."""
    attempt = Attempt.query.get_or_404(attempt_id)
    exam = _require_exam(attempt.exam_id)

    return render_template(
        "exams/attempt_report.html",
        papers=[{"attempt": attempt, "rows": _marked_paper(attempt)}],
        exam=exam,
        course=exam.course,
        single=True,
        show_key=request.args.get("key", "1") != "0",
        show_explanations=request.args.get("why", "1") != "0",
        printed_on=utcnow(),
    )


@bp.route("/<int:exam_id>/reports")
@staff_required
def exam_reports(exam_id: int):
    """Every student's marked paper, one per sheet, for handing back."""
    exam = _require_exam(exam_id)

    query = Attempt.query.filter_by(exam_id=exam.id)
    if request.args.get("submitted", "1") != "0":
        query = query.filter(Attempt.status == ATTEMPT_SUBMITTED)

    attempts = query.all()
    attempts.sort(key=lambda a: (a.student.sort_name if a.student else ""))

    papers = [
        {"attempt": attempt, "rows": _marked_paper(attempt)}
        for attempt in attempts
    ]

    return render_template(
        "exams/attempt_report.html",
        papers=papers,
        exam=exam,
        course=exam.course,
        single=False,
        show_key=request.args.get("key", "1") != "0",
        show_explanations=request.args.get("why", "1") != "0",
        printed_on=utcnow(),
    )


@bp.route("/attempts/<int:attempt_id>/reopen", methods=["POST"])
@staff_required
def attempt_reopen(attempt_id: int):
    """Give a student their time back without losing their answers.

    This is the "submitted by accident" rescue the instructor asked for.
    """
    attempt = Attempt.query.get_or_404(attempt_id)
    exam = _require_exam(attempt.exam_id)

    minutes = request.form.get("minutes", type=int) or 0
    attempt.status = ATTEMPT_IN_PROGRESS
    attempt.submitted_at = None
    attempt.auto_submitted = False
    attempt.submit_reason = ""
    attempt.reopen_count = (attempt.reopen_count or 0) + 1
    if minutes:
        attempt.extra_seconds = (attempt.extra_seconds or 0) + minutes * 60

    if not exam.is_open:
        exam.is_open = True
        exam.closed_at = None
        extra = minutes or exam.duration_minutes
        exam.closes_at = utcnow() + timedelta(minutes=extra)
        flash(
            f"The exam was reopened for {extra} minute(s) so this attempt can "
            f"continue. Close it again when they are done.",
            "info",
        )
    elif minutes and exam.closes_at:
        needed = utcnow() + timedelta(minutes=minutes)
        if needed > exam.closes_at:
            exam.closes_at = needed

    log_action(current_user, "attempt_reopened", "attempt", attempt.id,
               f"+{minutes}m")
    db.session.commit()
    flash(
        f"{attempt.student.display_name}'s attempt is open again with their "
        f"answers intact.",
        "success",
    )
    return redirect(url_for("exams.monitor", exam_id=exam.id))


@bp.route("/attempts/<int:attempt_id>/reset", methods=["POST"])
@staff_required
def attempt_reset(attempt_id: int):
    """Wipe the attempt entirely so the student starts fresh."""
    attempt = Attempt.query.get_or_404(attempt_id)
    exam = _require_exam(attempt.exam_id)
    name = attempt.student.display_name

    db.session.delete(attempt)
    log_action(current_user, "attempt_reset", "attempt", attempt_id, name)
    db.session.commit()
    flash(f"{name}'s attempt has been cleared. They can start over.", "success")
    return redirect(url_for("exams.monitor", exam_id=exam.id))


@bp.route("/attempts/<int:attempt_id>/override", methods=["POST"])
@staff_required
def attempt_override(attempt_id: int):
    """Instructor's figure wins, and survives any later re-marking."""
    attempt = Attempt.query.get_or_404(attempt_id)
    exam = _require_exam(attempt.exam_id)

    if request.form.get("action") == "clear":
        attempt.is_override = False
        attempt.override_score = None
        attempt.override_note = ""
    else:
        score = request.form.get("score", type=float)
        if score is None or score < 0:
            flash("Give a score of zero or more.", "error")
            return redirect(url_for("exams.attempt_detail", attempt_id=attempt.id))
        if attempt.total_points and score > attempt.total_points:
            flash(
                f"That is above the exam total of {attempt.total_points:g}. "
                f"Saved anyway — correct it if that was not intended.",
                "warning",
            )
        attempt.is_override = True
        attempt.override_score = score
        attempt.override_note = (request.form.get("note") or "").strip()

    _sync_to_gradebook(exam)
    log_action(current_user, "score_override", "attempt", attempt.id,
               f"score={attempt.override_score}")
    db.session.commit()
    flash("Score updated.", "success")
    return redirect(url_for("exams.attempt_detail", attempt_id=attempt.id))


@bp.route("/attempts/<int:attempt_id>/grade-essays", methods=["POST"])
@staff_required
def grade_essays(attempt_id: int):
    from ..marking import grade_essay

    attempt = Attempt.query.get_or_404(attempt_id)
    exam = _require_exam(attempt.exam_id)

    graded = 0
    for answer in attempt.answers:
        field = f"points_{answer.id}"
        if field not in request.form:
            continue
        points = request.form.get(field, type=float)
        if points is None:
            continue
        grade_essay(answer, points, request.form.get(f"feedback_{answer.id}", ""),
                    current_user)
        graded += 1

    mark_attempt(attempt, list(exam.questions))
    # Manual marks must not be wiped by the re-mark that follows them.
    attempt.score = round(sum((a.points_awarded or 0.0) for a in attempt.answers), 4)
    attempt.manual_pending = sum(1 for a in attempt.answers if a.needs_manual)
    _sync_to_gradebook(exam)

    log_action(current_user, "essays_graded", "attempt", attempt.id, f"{graded} items")
    db.session.commit()
    flash(f"Marked {graded} written answer(s).", "success")
    return redirect(url_for("exams.attempt_detail", attempt_id=attempt.id))


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

@bp.route("/<int:exam_id>/export.csv")
@staff_required
def export_csv(exam_id: int):
    import csv
    import io as _io

    from flask import Response

    exam = _require_exam(exam_id)
    buffer = _io.StringIO()
    writer = csv.writer(buffer)

    questions = list(exam.questions)
    header = ["Student number", "Name", "Section", "Status", "Started", "Submitted",
              "Score", "Total", "Percent", "Overridden", "Integrity events"]
    header += [f"Q{i}" for i in range(1, len(questions) + 1)]
    writer.writerow(header)

    section_names = {s.id: s.name for s in exam.course.sections}
    from ..models import Enrollment

    sections = {
        e.student_id: section_names.get(e.section_id, "")
        for e in Enrollment.query.filter_by(course_id=exam.course_id).all()
    }

    for attempt in Attempt.query.filter_by(exam_id=exam.id).all():
        by_question = {}
        for answer in attempt.answers:
            if answer.sub_question_id:
                by_question.setdefault(answer.question_id, []).append(
                    f"{answer.sub_question.label if answer.sub_question else ''}:"
                    f"{answer.points_awarded:g}"
                )
            else:
                by_question[answer.question_id] = [f"{answer.points_awarded or 0:g}"]

        row = [
            attempt.student.student_number or "",
            attempt.student.name,
            sections.get(attempt.student_id, ""),
            attempt.status,
            attempt.started_at.strftime("%Y-%m-%d %H:%M") if attempt.started_at else "",
            attempt.submitted_at.strftime("%Y-%m-%d %H:%M") if attempt.submitted_at else "",
            f"{attempt.effective_score:g}",
            f"{attempt.total_points or 0:g}",
            f"{attempt.percent:.2f}",
            "yes" if attempt.is_override else "",
            len(attempt.events),
        ]
        for question in questions:
            row.append(" ".join(by_question.get(question.id, [])))
        writer.writerow(row)

    filename = re.sub(r"[^A-Za-z0-9]+", "-", f"{exam.course.code}-{exam.title}").strip("-")
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )


@bp.route("/course/<int:course_id>/export.csv")
@staff_required
def export_course_csv(course_id: int):
    """Every exam result in the subject, one row per student per exam."""
    import csv
    import io as _io

    from flask import Response

    course = require_course(course_id)
    buffer = _io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Exam", "Category", "Date opened", "Student number", "Name",
         "Score", "Total", "Percent", "Status"]
    )

    for exam in Exam.query.filter_by(course_id=course.id).order_by(Exam.created_at).all():
        for attempt in Attempt.query.filter_by(exam_id=exam.id).all():
            writer.writerow(
                [
                    exam.title,
                    exam.category,
                    exam.opened_at.strftime("%Y-%m-%d") if exam.opened_at else "",
                    attempt.student.student_number or "",
                    attempt.student.name,
                    f"{attempt.effective_score:g}",
                    f"{attempt.total_points or 0:g}",
                    f"{attempt.percent:.2f}",
                    attempt.status,
                ]
            )

    filename = re.sub(r"[^A-Za-z0-9]+", "-", f"{course.code}-all-exams").strip("-")
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )


# --------------------------------------------------------------------------- #
# Small JSON helpers used by the authoring page
# --------------------------------------------------------------------------- #

@bp.route("/course/<int:course_id>/items.json")
@staff_required
def items_json(course_id: int):
    course = require_course(course_id)
    return jsonify(
        [
            {"id": item.id, "label": f"{item.component.name} — {item.title}",
             "max_points": item.max_points}
            for item in _linkable_items(course.id)
        ]
    )


@bp.route("/course/<int:course_id>/sections.json")
@staff_required
def sections_json(course_id: int):
    course = require_course(course_id)
    return jsonify([{"id": s.id, "name": s.name} for s in course.sections])
