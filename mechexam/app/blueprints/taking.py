"""Taking an exam.

Three things here were fought for and are worth stating plainly:

*   **One clock for the class.**  The deadline lives on the exam
    (``closes_at``), set when the instructor opens it, so every student is
    timed together and the exam shuts for everyone when it expires -- a
    student cannot gain time by signing in late.
*   **Answers are saved as they are typed**, through
    ``POST /take/<id>/autosave``.  A flat battery or a dropped connection
    costs the student nothing.
*   **Leaving the exam is distinguished from the screen going to sleep.**  A
    phone whose backlight times out fires the same browser events as a student
    switching to another app; the client checks ``document.hasFocus()`` to tell
    them apart, so strict mode submits a cheat and tolerates a dimmed screen.
"""
from __future__ import annotations

import random


from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

from ..extensions import db
from ..marking import mark_attempt
from ..models import (
    Answer,
    Attempt,
    ATTEMPT_IN_PROGRESS,
    ATTEMPT_SUBMITTED,
    Enrollment,
    Exam,
    EXIT_OFF,
    EXIT_STRICT,
    IntegrityEvent,
    Q_MATCHING,
    Question,
    Setting,
    log_action,
    utcnow,
)
from ..security import student_required

bp = Blueprint("taking", __name__)


# --------------------------------------------------------------------------- #
# Finding what is open
# --------------------------------------------------------------------------- #

@bp.route("/")
@student_required
def available():
    """Open exams, grouped by subject -- the sorted-per-subject view asked for."""
    enrolments = Enrollment.query.filter_by(
        student_id=current_user.id, is_dropped=False
    ).all()
    sections = {e.course_id: e.section_id for e in enrolments}
    course_ids = list(sections.keys())

    groups: list[dict] = []
    if course_ids:
        exams = (
            Exam.query.filter(Exam.course_id.in_(course_ids), Exam.is_open.is_(True))
            .order_by(Exam.opened_at.desc())
            .all()
        )
        by_course: dict[int, list] = {}
        for exam in exams:
            if exam.section_id and sections.get(exam.course_id) != exam.section_id:
                continue
            if exam.is_expired:
                continue
            attempt = Attempt.query.filter_by(
                exam_id=exam.id, student_id=current_user.id
            ).first()
            by_course.setdefault(exam.course_id, []).append(
                {"exam": exam, "attempt": attempt}
            )

        for enrolment in enrolments:
            entries = by_course.get(enrolment.course_id) or []
            if entries:
                groups.append({"course": enrolment.course, "entries": entries})

    history = (
        Attempt.query.filter_by(student_id=current_user.id, status=ATTEMPT_SUBMITTED)
        .order_by(Attempt.submitted_at.desc())
        .limit(20)
        .all()
    )
    return render_template("student/available.html", groups=groups, history=history)


def _enrolment_for(exam: Exam) -> Enrollment:
    enrolment = Enrollment.query.filter_by(
        course_id=exam.course_id, student_id=current_user.id, is_dropped=False
    ).first()
    if enrolment is None:
        abort(404)
    if exam.section_id and enrolment.section_id != exam.section_id:
        abort(404)
    return enrolment


# --------------------------------------------------------------------------- #
# Starting
# --------------------------------------------------------------------------- #

@bp.route("/<int:exam_id>/start", methods=["GET", "POST"])
@student_required
def start(exam_id: int):
    """The PIN is asked here, at the start of the exam, and nowhere else."""
    exam = Exam.query.get_or_404(exam_id)
    _enrolment_for(exam)

    attempt = Attempt.query.filter_by(
        exam_id=exam.id, student_id=current_user.id
    ).first()

    if attempt is not None and attempt.is_submitted:
        flash("You have already submitted this exam.", "info")
        return redirect(url_for("taking.review", attempt_id=attempt.id))

    if not exam.is_open:
        flash("That exam is not open.", "error")
        return redirect(url_for("taking.available"))

    if exam.is_expired:
        _expire(exam)
        flash("The time for that exam has run out.", "error")
        return redirect(url_for("taking.available"))

    if attempt is not None:
        return redirect(url_for("taking.sit", attempt_id=attempt.id))

    if request.method == "POST":
        code = (request.form.get("access_code") or "").strip()
        if exam.access_code and code.upper() != (exam.access_code or "").upper():
            flash("That exam PIN is not correct.", "error")
            return render_template("student/start.html", exam=exam), 403

        attempt = Attempt(
            exam_id=exam.id,
            student_id=current_user.id,
            status=ATTEMPT_IN_PROGRESS,
            started_at=utcnow(),
            last_seen_at=utcnow(),
            seed=random.randint(1, 2_000_000_000),
        )
        attempt.question_order = _order_for(exam, attempt.seed)
        db.session.add(attempt)
        log_action(current_user, "exam_started", "exam", exam.id)
        db.session.commit()
        return redirect(url_for("taking.sit", attempt_id=attempt.id))

    return render_template("student/start.html", exam=exam)


def _order_for(exam: Exam, seed: int) -> list[int]:
    """Per-student question order, reproducible from the stored seed."""
    ids = [q.id for q in exam.questions]
    if exam.shuffle_questions:
        rng = random.Random(seed)
        rng.shuffle(ids)
    if exam.bank_draw_count and 0 < exam.bank_draw_count < len(ids):
        ids = ids[: exam.bank_draw_count]
    return ids


def _expire(exam: Exam) -> None:
    """Time is up: close the exam and submit everyone still in it."""
    exam.is_open = False
    exam.closed_at = utcnow()
    questions = list(exam.questions)
    for attempt in Attempt.query.filter_by(
        exam_id=exam.id, status=ATTEMPT_IN_PROGRESS
    ).all():
        attempt.status = ATTEMPT_SUBMITTED
        attempt.submitted_at = utcnow()
        attempt.auto_submitted = True
        attempt.submit_reason = "time_expired"
        mark_attempt(attempt, questions)
        db.session.add(
            IntegrityEvent(
                attempt_id=attempt.id, kind="auto_submit", detail="time expired"
            )
        )
    db.session.commit()

    from .exams import _sync_to_gradebook

    _sync_to_gradebook(exam)
    db.session.commit()


def _submit_now(attempt: Attempt, reason: str, detail: str, questions) -> None:
    """End one attempt where it stands."""
    attempt.status = ATTEMPT_SUBMITTED
    attempt.submitted_at = utcnow()
    attempt.auto_submitted = True
    attempt.submit_reason = reason
    attempt.away_since = None
    mark_attempt(attempt, questions)
    db.session.add(
        IntegrityEvent(attempt_id=attempt.id, kind="auto_submit", detail=detail)
    )


def sweep_exam(exam: Exam) -> int:
    """Submit the attempts that have run out of rope, and say how many.

    This is the half that makes leaving an exam actually end it. The client
    decides nothing while it is away -- it may not be running at all, because
    a phone that is switched off or a browser that the system has frozen sends
    nothing and runs no timers. So the server keeps the clock instead: the
    client reports the moment the exam left the screen, and from then on this
    sweep can end the attempt whether or not that device is ever heard from
    again.

    It is called from the pages that are already being polled -- the
    instructor's monitor, and every other student's clock check -- so no
    background job is needed on a host that offers none.
    """
    if not exam.is_open or exam.exit_mode == EXIT_OFF:
        return 0

    now = utcnow()
    allowance = max(5, exam.lock_grace_seconds or 90)
    idle_limit = exam.idle_seconds or 0
    questions = None
    submitted = 0

    for attempt in Attempt.query.filter_by(
        exam_id=exam.id, status=ATTEMPT_IN_PROGRESS
    ).all():
        reason = detail = ""

        if attempt.away_since is not None:
            away = int((now - attempt.away_since).total_seconds())
            if away >= allowance:
                reason = "exit:away"
                detail = (
                    f"away from the exam for {away}s, over the "
                    f"{allowance}s allowed"
                )

        if not reason and idle_limit:
            since = attempt.last_activity_at or attempt.started_at
            if since is not None:
                idle = int((now - since).total_seconds())
                if idle >= idle_limit:
                    reason = "idle"
                    detail = (
                        f"no activity for {idle}s, over the {idle_limit}s "
                        f"allowed"
                    )

        if not reason:
            continue

        if questions is None:
            questions = list(exam.questions)
        _submit_now(attempt, reason, detail, questions)
        submitted += 1

    if submitted:
        db.session.commit()
        from .exams import _sync_to_gradebook

        _sync_to_gradebook(exam)
        db.session.commit()
    return submitted


# --------------------------------------------------------------------------- #
# Sitting
# --------------------------------------------------------------------------- #

def _require_attempt(attempt_id: int) -> Attempt:
    attempt = Attempt.query.get_or_404(attempt_id)
    if attempt.student_id != current_user.id:
        abort(404)
    return attempt


@bp.route("/attempt/<int:attempt_id>")
@student_required
def sit(attempt_id: int):
    attempt = _require_attempt(attempt_id)
    exam = attempt.exam

    if attempt.is_submitted:
        return redirect(url_for("taking.review", attempt_id=attempt.id))

    if exam.is_expired:
        _expire(exam)
        flash("Time is up — your answers were submitted automatically.", "info")
        return redirect(url_for("taking.review", attempt_id=attempt.id))

    if not exam.is_open:
        flash("The instructor has closed this exam.", "info")
        return redirect(url_for("taking.available"))

    questions = _questions_in_order(attempt, exam)
    saved = _saved_answers(attempt)
    layouts = {q.id: _layout(q, attempt.seed, exam) for q in questions}

    # Opening the exam page is unambiguously being present, so any absence
    # the server was timing ends here.
    attempt.last_seen_at = utcnow()
    attempt.last_activity_at = utcnow()
    attempt.away_since = None
    db.session.commit()

    remaining = exam.seconds_remaining + (attempt.extra_seconds or 0)

    return render_template(
        "student/sit.html",
        attempt=attempt,
        exam=exam,
        questions=questions,
        saved=saved,
        layouts=layouts,
        seconds_remaining=remaining,
        autosave_seconds=Setting.get_bounded("exam_autosave_seconds"),
        clock_poll_seconds=Setting.get_bounded("exam_clock_poll_seconds"),
    )


def _questions_in_order(attempt: Attempt, exam: Exam) -> list[Question]:
    order = attempt.question_order
    by_id = {q.id: q for q in exam.questions}
    if not order:
        order = [q.id for q in exam.questions]
        attempt.question_order = order
    return [by_id[qid] for qid in order if qid in by_id]


def _layout(question: Question, seed: int, exam: Exam) -> dict:
    """What the student sees: option order, and the shuffled right column."""
    rng = random.Random(seed * 31 + question.id)
    layout: dict = {}

    if question.qtype in ("mcq",) and exam.shuffle_options:
        options = list(question.options)
        rng.shuffle(options)
        layout["options"] = options
    elif question.qtype == "mcq":
        layout["options"] = list(question.options)

    if question.qtype == Q_MATCHING:
        pairs = question.pairs
        right = [
            {"index": index, "text": pair.get("right", "")}
            for index, pair in enumerate(pairs)
        ]
        if exam.shuffle_options:
            rng.shuffle(right)
        layout["left"] = [
            {"index": index, "text": pair.get("left", "")}
            for index, pair in enumerate(pairs)
        ]
        layout["right"] = right

    return layout


def _saved_answers(attempt: Attempt) -> dict:
    """Keyed ``qN`` and ``qN_sM`` so the template can look either up."""
    out: dict[str, str] = {}
    for answer in attempt.answers:
        if answer.sub_question_id:
            out[f"q{answer.question_id}_s{answer.sub_question_id}"] = answer.response or ""
        else:
            out[f"q{answer.question_id}"] = answer.response or ""
    return out


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #

def _store_answers(attempt: Attempt, form) -> int:
    """Write whatever the form carries, without marking it yet."""
    exam = attempt.exam
    questions = {q.id: q for q in exam.questions}
    stored = 0

    existing = {
        (a.question_id, a.sub_question_id): a for a in attempt.answers
    }

    for key, value in form.items():
        if not key.startswith("q"):
            continue
        body = key[1:]
        sub_id = None
        if "_s" in body:
            head, tail = body.split("_s", 1)
            if not head.isdigit() or not tail.isdigit():
                continue
            question_id, sub_id = int(head), int(tail)
        elif body.isdigit():
            question_id = int(body)
        else:
            continue

        question = questions.get(question_id)
        if question is None:
            continue

        row = existing.get((question_id, sub_id))
        if row is None:
            row = Answer(
                attempt_id=attempt.id,
                question_id=question_id,
                sub_question_id=sub_id,
            )
            db.session.add(row)
            existing[(question_id, sub_id)] = row

        row.response = value if isinstance(value, str) else str(value)
        stored += 1

    # Matching questions arrive as one field per left-hand item.
    for question in exam.questions:
        if question.qtype != Q_MATCHING:
            continue
        lines = []
        for index in range(len(question.pairs)):
            field = f"match_{question.id}_{index}"
            if field in form:
                chosen = (form.get(field) or "").strip()
                if chosen != "":
                    lines.append(f"{index}={chosen}")
        if lines or any(k.startswith(f"match_{question.id}_") for k in form):
            row = existing.get((question.id, None))
            if row is None:
                row = Answer(attempt_id=attempt.id, question_id=question.id)
                db.session.add(row)
                existing[(question.id, None)] = row
            row.response = "\n".join(lines)
            stored += 1

    return stored


@bp.route("/attempt/<int:attempt_id>/autosave", methods=["POST"])
@student_required
def autosave(attempt_id: int):
    attempt = _require_attempt(attempt_id)
    if attempt.is_submitted:
        return jsonify({"ok": False, "reason": "submitted"}), 409

    exam = attempt.exam
    if exam.is_expired:
        _expire(exam)
        return jsonify({"ok": False, "reason": "expired",
                        "redirect": url_for("taking.review", attempt_id=attempt.id)}), 409

    _store_answers(attempt, request.form)
    attempt.last_seen_at = utcnow()
    # A save sent as the page goes away is a farewell, not a sign of life: it
    # arrives just after the event that reported the absence, and clearing the
    # clock here would cancel the absence it was sent during.
    if not request.form.get("leaving"):
        attempt.last_activity_at = utcnow()
        attempt.away_since = None
    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "saved_at": utcnow().strftime("%H:%M:%S"),
            "seconds_remaining": exam.seconds_remaining + (attempt.extra_seconds or 0),
        }
    )


@bp.route("/attempt/<int:attempt_id>/event", methods=["POST"])
@student_required
def event(attempt_id: int):
    """Record an integrity event, and decide whether to submit.

    Only one kind of event can end an exam: ``exit_confirmed``, which the
    client sends when a student has come back after an absence too long to be
    a locked screen, having already used up the exam's allowance.

    Everything else is recorded and nothing more.  ``blur`` in particular is
    never acted on: a phone locking its screen blurs the exam window exactly
    as switching apps does, and an earlier version of this endpoint submitted
    on a bare blur, which is why a student who merely let the screen time out
    found the exam gone.  The decision now needs a duration, and a duration is
    only known once the student is back.
    """
    attempt = _require_attempt(attempt_id)
    exam = attempt.exam

    kind = (request.form.get("kind") or "").strip()[:40]
    detail = (request.form.get("detail") or "").strip()[:250]
    if not kind:
        return jsonify({"ok": False}), 400

    if attempt.is_submitted:
        return jsonify({"ok": True, "action": "none"})

    db.session.add(
        IntegrityEvent(attempt_id=attempt.id, kind=kind, detail=detail)
    )

    # The server keeps its own clock on an absence, because the client may
    # stop running the moment it starts: a locked phone fires no timers.
    if kind in ("hidden", "screen_sleep"):
        if attempt.away_since is None:
            attempt.away_since = utcnow()
    elif kind in ("interruption", "return", "absence_forgiven", "visible",
                  "resume"):
        attempt.away_since = None
        attempt.last_activity_at = utcnow()

    action = "none"
    if kind == "exit_confirmed" and exam.exit_mode == EXIT_STRICT:
        _store_answers(attempt, request.form)
        attempt.status = ATTEMPT_SUBMITTED
        attempt.submitted_at = utcnow()
        attempt.auto_submitted = True
        attempt.submit_reason = f"exit:{kind}"
        mark_attempt(attempt, list(exam.questions))
        db.session.add(
            IntegrityEvent(
                attempt_id=attempt.id,
                kind="auto_submit",
                detail=f"strict mode — {detail or kind}",
            )
        )
        action = "submitted"

    db.session.commit()

    if action == "submitted":
        from .exams import _sync_to_gradebook

        _sync_to_gradebook(exam)
        db.session.commit()

    return jsonify(
        {
            "ok": True,
            "action": action,
            "redirect": url_for("taking.review", attempt_id=attempt.id)
            if action == "submitted"
            else None,
        }
    )


@bp.route("/attempt/<int:attempt_id>/submit", methods=["POST"])
@student_required
def submit(attempt_id: int):
    attempt = _require_attempt(attempt_id)
    exam = attempt.exam

    if attempt.is_submitted:
        return redirect(url_for("taking.review", attempt_id=attempt.id))

    _store_answers(attempt, request.form)
    attempt.status = ATTEMPT_SUBMITTED
    attempt.submitted_at = utcnow()
    attempt.auto_submitted = bool(request.form.get("auto"))
    attempt.submit_reason = (request.form.get("reason") or "manual")[:60]
    mark_attempt(attempt, list(exam.questions))

    log_action(current_user, "exam_submitted", "attempt", attempt.id,
               f"{attempt.score}/{attempt.total_points}")
    db.session.commit()

    from .exams import _sync_to_gradebook

    _sync_to_gradebook(exam)
    db.session.commit()

    flash("Your answers have been submitted.", "success")
    return redirect(url_for("taking.review", attempt_id=attempt.id))


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #

@bp.route("/attempt/<int:attempt_id>/review")
@student_required
def review(attempt_id: int):
    attempt = _require_attempt(attempt_id)
    exam = attempt.exam

    answers: dict[int, list] = {}
    for answer in attempt.answers:
        answers.setdefault(answer.question_id, []).append(answer)

    questions = _questions_in_order(attempt, exam)

    return render_template(
        "student/review.html",
        attempt=attempt,
        exam=exam,
        questions=questions,
        answers=answers,
        show_score=exam.show_score,
        show_answers=exam.show_answers and attempt.is_submitted,
    )


@bp.route("/attempt/<int:attempt_id>/status.json")
@student_required
def status(attempt_id: int):
    """Polled by the exam page so a class-wide close takes effect at once.

    It also reports whether this student has touched anything since the last
    poll, and runs the sweep, so an absence is acted on even when the only
    device still running is somebody else's.
    """
    attempt = _require_attempt(attempt_id)
    exam = attempt.exam

    if request.args.get("active") == "1":
        attempt.last_activity_at = utcnow()
        attempt.away_since = None
    attempt.last_seen_at = utcnow()
    db.session.commit()

    sweep_exam(exam)
    db.session.refresh(attempt)

    return jsonify(
        {
            "open": bool(exam.is_open and not exam.is_expired),
            "submitted": attempt.is_submitted,
            "seconds_remaining": exam.seconds_remaining + (attempt.extra_seconds or 0),
            "redirect": url_for("taking.review", attempt_id=attempt.id)
            if attempt.is_submitted
            else None,
        }
    )
