"""Carrying an examination's results from the laptop back to the main portal.

An examination run on the room's own network leaves the marks on the laptop.
They have to get back to the portal the rest of the term lives on, and doing
that by hand -- backup, download, restore -- replaces the whole database and
destroys anything entered on the web in the meantime. This moves only what
the examination produced, and merges it.

**Matching without shared identifiers.** The two databases number their rows
independently, so exam 5 on the laptop is not exam 5 on the web. Everything
is therefore matched on what it actually is: a student by student number, a
subject by code and term, an exam by its subject and title, an attempt by its
exam and student, an answer by its attempt and question. Nothing relies on an
integer that means different things on the two machines.

**Conservative by rule.** The laptop is authoritative for what happened
during the examination and for nothing else:

* A student who exists on the web keeps their name and password; only a
  student who is not there at all is created.
* An attempt is written only when the web has none, or has one still in
  progress. An attempt already submitted on the web is left alone and
  reported, because the instructor may have marked an essay or adjusted a
  score there since.
* A gradebook mark is written when the web has none, or when the laptop's is
  newer. An equal timestamp changes nothing.
* Final grades are never sent. Finalising a subject is a deliberate act and
  belongs where the records are kept.
* Nothing is ever deleted.

**Repeatable.** Sending the same package twice has the same effect as sending
it once, so a retry after a dropped connection is safe and needs no
bookkeeping.
"""
from __future__ import annotations

import json
import platform
from datetime import datetime

from .extensions import db
from .models import (
    Answer,
    Attempt,
    Course,
    Enrollment,
    Exam,
    GradeComponent,
    GradeItem,
    IntegrityEvent,
    ROLE_STUDENT,
    Score,
    Section,
    Setting,
    User,
    utcnow,
)

FORMAT = 2

SETTING_URL = "sync_url"
SETTING_TOKEN = "sync_token"
SETTING_ENABLED = "sync_enabled"
SETTING_LAST = "sync_last_result"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _when(value) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _read(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _student_key(user: User) -> str:
    """What identifies a student across two databases."""
    return (user.student_number or user.username or "").strip().lower()


# --------------------------------------------------------------------------- #
# Building a package on the laptop
# --------------------------------------------------------------------------- #

def build_package(course: Course, exams: list[Exam] | None = None) -> dict:
    """Everything one subject's examinations produced, ready to send."""
    chosen = list(exams) if exams is not None else list(
        Exam.query.filter_by(course_id=course.id).all()
    )

    students: dict[str, dict] = {}
    sections = {s.id: s.name for s in course.sections}

    def remember(user: User, section_id=None) -> str:
        key = _student_key(user)
        if key and key not in students:
            students[key] = {
                "student_number": user.student_number or "",
                "username": user.username,
                "name": user.name,
                "program": user.program or "",
                "year_level": user.year_level or "",
                "section": sections.get(section_id, "") if section_id else "",
            }
        return key

    for enrolment in Enrollment.query.filter_by(course_id=course.id).all():
        if enrolment.student is not None:
            remember(enrolment.student, enrolment.section_id)

    exam_rows = []
    for exam in chosen:
        questions = sorted(exam.questions, key=lambda q: (q.order_index or 0, q.id))
        order = {q.id: index for index, q in enumerate(questions)}
        labels = {}
        for question in questions:
            for part in question.parts:
                labels[part.id] = (order[question.id], part.label or "")

        attempts = []
        for attempt in Attempt.query.filter_by(exam_id=exam.id).all():
            if attempt.student is None:
                continue
            key = remember(attempt.student)
            answers = []
            for answer in attempt.answers:
                if answer.question_id not in order:
                    continue
                answers.append({
                    "question": order[answer.question_id],
                    "part": labels.get(answer.sub_question_id, (None, ""))[1]
                            if answer.sub_question_id else "",
                    "response": answer.response or "",
                    "is_correct": answer.is_correct,
                    "points_awarded": answer.points_awarded,
                    "max_points": answer.max_points,
                    "needs_manual": bool(answer.needs_manual),
                    "feedback": answer.feedback or "",
                    "graded_at": _when(answer.graded_at),
                    "updated_at": _when(answer.updated_at),
                })

            attempts.append({
                "student": key,
                "status": attempt.status,
                "started_at": _when(attempt.started_at),
                "submitted_at": _when(attempt.submitted_at),
                "auto_submitted": bool(attempt.auto_submitted),
                "submit_reason": attempt.submit_reason or "",
                "score": attempt.score,
                "total_points": attempt.total_points,
                "manual_pending": attempt.manual_pending or 0,
                "is_override": bool(attempt.is_override),
                "override_score": attempt.override_score,
                "override_note": attempt.override_note or "",
                "seed": attempt.seed or 0,
                "reopen_count": attempt.reopen_count or 0,
                "extra_seconds": attempt.extra_seconds or 0,
                "answers": answers,
                "events": [
                    {"kind": e.kind, "detail": e.detail or "",
                     "created_at": _when(e.created_at)}
                    for e in attempt.events
                ],
            })

        exam_rows.append({
            "title": exam.title,
            "category": exam.category or "",
            "school_year": exam.school_year or "",
            "semester": exam.semester or "",
            "question_count": len(questions),
            "attempts": attempts,
        })

    # Gradebook marks, so anything typed on the laptop travels too.
    marks = []
    for component in course.components:
        for item in component.items:
            for score in Score.query.filter_by(item_id=item.id).all():
                if score.student is None:
                    continue
                marks.append({
                    "scope": component.scope,
                    "component": component.name,
                    "item": item.title,
                    "max_points": item.max_points,
                    "item_type": item.item_type or "",
                    "student": remember(score.student),
                    "points": score.points,
                    "is_override": bool(score.is_override),
                    "override_note": score.override_note or "",
                    "remarks": score.remarks or "",
                    "updated_at": _when(score.updated_at),
                })

    return {
        "format": FORMAT,
        "built_at": _when(utcnow()),
        "source": platform.node() or "a laptop",
        "course": {
            "code": course.code,
            "title": course.title,
            "school_year": course.school_year or "",
            "semester": course.semester or "",
        },
        "students": students,
        "exams": exam_rows,
        "marks": marks,
    }


# --------------------------------------------------------------------------- #
# Applying a package on the main portal
# --------------------------------------------------------------------------- #

def apply_package(package: dict) -> dict:
    """Merge a package into this database. Returns a plain-words report."""
    report = {
        "students_created": 0,
        "attempts_written": 0,
        "attempts_skipped": 0,
        "answers_written": 0,
        "events_written": 0,
        "marks_written": 0,
        "marks_skipped": 0,
        "notes": [],
    }

    if int(package.get("format") or 0) > FORMAT:
        report["notes"].append(
            "This package was made by a newer version of the portal. Update "
            "this copy before sending again."
        )
        return report

    spec = package.get("course") or {}
    course = Course.query.filter_by(
        code=spec.get("code"),
        school_year=spec.get("school_year"),
        semester=spec.get("semester"),
    ).first()
    if course is None:
        report["notes"].append(
            f"No subject {spec.get('code')} for {spec.get('semester')}, "
            f"{spec.get('school_year')} exists here, so nothing was merged. "
            f"Create it first, or restore a backup."
        )
        return report

    # ---- students -------------------------------------------------------- #
    people: dict[str, User] = {}
    for key, row in (package.get("students") or {}).items():
        number = (row.get("student_number") or "").strip()
        user = None
        if number:
            user = User.query.filter(
                db.func.lower(User.student_number) == number.lower()
            ).first()
        if user is None:
            username = (row.get("username") or number or key).strip()
            user = User.query.filter(
                db.func.lower(User.username) == username.lower()
            ).first()

        if user is None:
            user = User(
                username=(row.get("username") or number or key),
                student_number=number,
                name=row.get("name") or number,
                role=ROLE_STUDENT,
                program=row.get("program") or "",
                year_level=row.get("year_level") or "",
            )
            user.set_password(number or user.username)
            user.must_change_password = Setting.get_bool(
                "new_students_must_change_password"
            )
            db.session.add(user)
            db.session.flush()
            report["students_created"] += 1

        people[key] = user

        section = None
        if row.get("section"):
            section = Section.query.filter_by(
                course_id=course.id, name=row["section"]
            ).first()
        existing = Enrollment.query.filter_by(
            course_id=course.id, student_id=user.id
        ).first()
        if existing is None:
            db.session.add(Enrollment(
                course_id=course.id, student_id=user.id,
                section_id=section.id if section else None,
            ))

    db.session.flush()

    # ---- examinations ----------------------------------------------------- #
    for exam_row in package.get("exams") or []:
        exam = Exam.query.filter_by(
            course_id=course.id, title=exam_row.get("title")
        ).first()
        if exam is None:
            report["notes"].append(
                f"No examination called “{exam_row.get('title')}” exists here, "
                f"so its {len(exam_row.get('attempts') or [])} attempt(s) were "
                f"not merged. The paper itself has to exist on both sides."
            )
            continue

        questions = sorted(exam.questions, key=lambda q: (q.order_index or 0, q.id))
        if len(questions) != exam_row.get("question_count"):
            report["notes"].append(
                f"“{exam.title}” has {len(questions)} question(s) here but "
                f"{exam_row.get('question_count')} on the laptop. Its attempts "
                f"were not merged, because the answers could not be matched to "
                f"the right questions."
            )
            continue

        for attempt_row in exam_row.get("attempts") or []:
            student = people.get(attempt_row.get("student"))
            if student is None:
                report["attempts_skipped"] += 1
                continue

            attempt = Attempt.query.filter_by(
                exam_id=exam.id, student_id=student.id
            ).first()

            if attempt is not None and attempt.is_submitted:
                report["attempts_skipped"] += 1
                report["notes"].append(
                    f"{student.name or student.username} already has a "
                    f"submitted attempt at “{exam.title}” here, so the "
                    f"laptop's was not merged over it."
                )
                continue

            if attempt is None:
                attempt = Attempt(exam_id=exam.id, student_id=student.id)
                db.session.add(attempt)
                db.session.flush()
            else:
                Answer.query.filter_by(attempt_id=attempt.id).delete()
                IntegrityEvent.query.filter_by(attempt_id=attempt.id).delete()

            attempt.status = attempt_row.get("status") or attempt.status
            attempt.started_at = _read(attempt_row.get("started_at"))
            attempt.submitted_at = _read(attempt_row.get("submitted_at"))
            attempt.auto_submitted = bool(attempt_row.get("auto_submitted"))
            attempt.submit_reason = attempt_row.get("submit_reason") or ""
            attempt.score = attempt_row.get("score")
            attempt.total_points = attempt_row.get("total_points")
            attempt.manual_pending = attempt_row.get("manual_pending") or 0
            attempt.is_override = bool(attempt_row.get("is_override"))
            attempt.override_score = attempt_row.get("override_score")
            attempt.override_note = attempt_row.get("override_note") or ""
            attempt.seed = attempt_row.get("seed") or 0
            attempt.reopen_count = attempt_row.get("reopen_count") or 0
            attempt.extra_seconds = attempt_row.get("extra_seconds") or 0
            attempt.question_order = [q.id for q in questions]
            report["attempts_written"] += 1

            for answer_row in attempt_row.get("answers") or []:
                index = answer_row.get("question")
                if not isinstance(index, int) or not 0 <= index < len(questions):
                    continue
                question = questions[index]

                sub_id = None
                if answer_row.get("part"):
                    part = next(
                        (p for p in question.parts
                         if (p.label or "") == answer_row["part"]), None
                    )
                    if part is None:
                        continue
                    sub_id = part.id

                db.session.add(Answer(
                    attempt_id=attempt.id,
                    question_id=question.id,
                    sub_question_id=sub_id,
                    response=answer_row.get("response") or "",
                    is_correct=answer_row.get("is_correct"),
                    points_awarded=answer_row.get("points_awarded") or 0.0,
                    max_points=answer_row.get("max_points") or 0.0,
                    needs_manual=bool(answer_row.get("needs_manual")),
                    feedback=answer_row.get("feedback") or "",
                    graded_at=_read(answer_row.get("graded_at")),
                ))
                report["answers_written"] += 1

            for event_row in attempt_row.get("events") or []:
                db.session.add(IntegrityEvent(
                    attempt_id=attempt.id,
                    kind=(event_row.get("kind") or "")[:40],
                    detail=(event_row.get("detail") or "")[:250],
                    created_at=_read(event_row.get("created_at")) or utcnow(),
                ))
                report["events_written"] += 1

    # ---- gradebook marks --------------------------------------------------- #
    for mark in package.get("marks") or []:
        student = people.get(mark.get("student"))
        if student is None:
            continue

        component = GradeComponent.query.filter_by(
            course_id=course.id, scope=mark.get("scope"), name=mark.get("component")
        ).first()
        if component is None:
            report["marks_skipped"] += 1
            continue

        item = GradeItem.query.filter_by(
            component_id=component.id, title=mark.get("item")
        ).first()
        if item is None:
            item = GradeItem(
                component_id=component.id,
                title=mark.get("item"),
                item_type=mark.get("item_type") or "quiz",
                max_points=mark.get("max_points") or 100.0,
            )
            db.session.add(item)
            db.session.flush()

        score = Score.query.filter_by(item_id=item.id, student_id=student.id).first()
        incoming = _read(mark.get("updated_at"))
        if score is not None:
            here = score.updated_at
            if here is not None and incoming is not None and here >= incoming:
                report["marks_skipped"] += 1
                continue
        else:
            score = Score(item_id=item.id, student_id=student.id)
            db.session.add(score)

        score.points = mark.get("points")
        score.is_override = bool(mark.get("is_override"))
        score.override_note = mark.get("override_note") or ""
        score.remarks = mark.get("remarks") or ""
        report["marks_written"] += 1

    db.session.commit()
    return report


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #

def is_configured() -> bool:
    return bool((Setting.get(SETTING_URL, "") or "").strip()
                and (Setting.get(SETTING_TOKEN, "") or "").strip())


def send(package: dict, timeout: int = 60) -> dict:
    """Push one package to the main portal. Never raises."""
    import urllib.error
    import urllib.request

    url = (Setting.get(SETTING_URL, "") or "").strip()
    token = (Setting.get(SETTING_TOKEN, "") or "").strip()
    if not url or not token:
        return {"ok": False, "error": "No main portal address or token is set."}

    body = json.dumps(package).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/api/sync",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-sync-token": token,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {"ok": True, "report": payload.get("report") or {}}
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = json.loads(error.read().decode("utf-8")).get("error", "")
        except Exception:                               # noqa: BLE001
            detail = error.reason or ""
        if error.code in (401, 403):
            return {"ok": False, "error":
                    "The main portal refused the token. Check that the same "
                    "token is set on both machines."}
        return {"ok": False, "error": f"The main portal said {error.code}: {detail}"}
    except urllib.error.URLError as error:
        return {"ok": False, "offline": True,
                "error": f"Could not reach the main portal ({error.reason}). "
                         f"It will try again."}
    except Exception as error:                          # noqa: BLE001
        return {"ok": False, "error": str(error)}


def remember_result(result: dict) -> None:
    Setting.put(SETTING_LAST, json.dumps({
        "at": _when(utcnow()),
        "ok": bool(result.get("ok")),
        "error": result.get("error", ""),
        "report": result.get("report") or {},
    })[:4000])
    db.session.commit()


def last_result() -> dict:
    try:
        return json.loads(Setting.get(SETTING_LAST, "") or "{}")
    except json.JSONDecodeError:
        return {}
