"""Automatic marking of exam answers.

Numerical answers are compared with a per-question percentage tolerance, which
is what engineering problems need -- a student who computes 101.3 kPa against a
key of 101.325 kPa is right.  Short answers are compared after normalisation
with an accepted-answer list.  Essays are never auto-marked; they are queued
for the instructor, with a rubric when one is attached.
"""
from __future__ import annotations

import re
import unicodedata

from .models import (
    Answer,
    Attempt,
    Q_ESSAY,
    Q_MATCHING,
    Q_MCQ,
    Q_NUMERIC,
    Q_SHORT,
    Q_TRUEFALSE,
    Question,
    SubQuestion,
    utcnow,
)

_NUMBER = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

def normalise_text(raw: str) -> str:
    """Case, accent, punctuation and whitespace insensitive comparison form."""
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKD", str(raw))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = re.sub(r"[^\w\s./-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_number(raw: str) -> float | None:
    """Pull a number out of a student's answer.

    Tolerates thousands separators, a trailing unit, scientific notation and
    the common ``x10^3`` shorthand.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    text = text.replace(",", "").replace("−", "-").replace("–", "-")
    text = re.sub(r"\s*[x×]\s*10\s*\^?\s*([-+]?\d+)", r"e\1", text)
    text = text.replace("^", "e") if re.fullmatch(r"[-+]?\d+\.?\d*\^[-+]?\d+", text) else text

    try:
        return float(text)
    except ValueError:
        pass

    match = _NUMBER.search(text)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return None
    return None


def accepted_answers(raw: str) -> list[str]:
    """Split a key on ``|`` or newlines into a list of accepted answers."""
    if not raw:
        return []
    parts = re.split(r"[|\n]", str(raw))
    return [p.strip() for p in parts if p.strip()]


# --------------------------------------------------------------------------- #
# Per-type marking
# --------------------------------------------------------------------------- #

def mark_numeric(response: str, key: str, tolerance_percent: float | None) -> bool:
    expected = parse_number(key)
    given = parse_number(response)
    if expected is None or given is None:
        return False

    tolerance = float(tolerance_percent or 0.0)
    if tolerance <= 0:
        # Exact to a sensible number of significant figures.
        return abs(given - expected) <= max(abs(expected) * 1e-9, 1e-9)

    if expected == 0:
        return abs(given) <= tolerance / 100.0
    return abs(given - expected) / abs(expected) * 100.0 <= tolerance + 1e-9


def mark_short(response: str, key: str) -> bool:
    given = normalise_text(response)
    if not given:
        return False
    for candidate in accepted_answers(key):
        if normalise_text(candidate) == given:
            return True
    return False


def mark_choice(response: str, key: str) -> bool:
    given = (response or "").strip()
    expected = (key or "").strip()
    if not given or not expected:
        return False
    if given == expected:
        return True
    return normalise_text(given) == normalise_text(expected)


def mark_matching(response: str, question: Question) -> tuple[float, float]:
    """Matching type: part marks, ``points`` per correct pair.

    The response is stored as ``left index -> chosen right index``, one per
    line, e.g. ``0=2``.
    """
    pairs = question.pairs or []
    per_pair = question.points if question.points is not None else 1.0
    possible = per_pair * len(pairs)
    if not pairs:
        return 0.0, 0.0

    chosen: dict[str, str] = {}
    for line in str(response or "").splitlines():
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        chosen[left.strip()] = right.strip()

    earned = 0.0
    for index, pair in enumerate(pairs):
        want = str(pair.get("answer", index))
        if chosen.get(str(index), "") == want:
            earned += per_pair
    return earned, possible


def matching_detail(response: str, question: Question) -> list[dict]:
    """Row-by-row account of a matching answer, for review pages.

    Parsing this in a template is possible but unreadable and easy to get
    wrong, so it is done here and handed over ready to display.
    """
    pairs = question.pairs or []
    chosen: dict[str, str] = {}
    for line in str(response or "").splitlines():
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        chosen[left.strip()] = right.strip()

    rows = []
    for index, pair in enumerate(pairs):
        picked = chosen.get(str(index), "")
        picked_text = ""
        if picked != "":
            try:
                position = int(picked)
                if 0 <= position < len(pairs):
                    picked_text = pairs[position].get("right", "")
            except (TypeError, ValueError):
                picked_text = picked

        rows.append(
            {
                "left": pair.get("left", ""),
                "correct": pair.get("right", ""),
                "chose": picked_text,
                "is_correct": picked == str(pair.get("answer", index)),
                "answered": picked != "",
            }
        )
    return rows


def mark_sub_question(response: str, part: SubQuestion) -> bool:
    kind = (part.answer_type or Q_NUMERIC).lower()
    if kind in (Q_NUMERIC, "num", "number", "numeric"):
        return mark_numeric(response, part.answer or "", part.tolerance_percent)
    if kind in (Q_MCQ, "choice"):
        return mark_choice(response, part.answer or "")
    if kind == Q_TRUEFALSE:
        return mark_choice(response, part.answer or "")
    return mark_short(response, part.answer or "")


# --------------------------------------------------------------------------- #
# Attempt-level marking
# --------------------------------------------------------------------------- #

def mark_answer(answer: Answer) -> None:
    """Score one stored answer in place."""
    question = answer.question
    if question is None:
        return

    if answer.sub_question_id and answer.sub_question is not None:
        part = answer.sub_question
        answer.max_points = part.points or 0.0
        answer.needs_manual = False
        correct = mark_sub_question(answer.response or "", part)
        answer.is_correct = correct
        answer.points_awarded = answer.max_points if correct else 0.0
        return

    qtype = question.qtype

    if qtype == Q_ESSAY:
        answer.max_points = question.points or 0.0
        answer.needs_manual = True
        answer.is_correct = None
        # Leave whatever the instructor has already awarded.
        answer.points_awarded = answer.points_awarded or 0.0
        return

    if qtype == Q_MATCHING:
        earned, possible = mark_matching(answer.response or "", question)
        answer.max_points = possible
        answer.points_awarded = round(earned, 4)
        answer.is_correct = possible > 0 and earned >= possible
        answer.needs_manual = False
        return

    answer.max_points = question.points or 0.0
    answer.needs_manual = False

    if qtype == Q_NUMERIC:
        correct = mark_numeric(answer.response or "", question.answer or "",
                               question.tolerance_percent)
    elif qtype == Q_SHORT:
        correct = mark_short(answer.response or "", question.answer or "")
    else:  # mcq, truefalse
        correct = mark_choice(answer.response or "", question.answer or "")

    answer.is_correct = correct
    answer.points_awarded = answer.max_points if correct else 0.0


def mark_attempt(attempt: Attempt, questions: list[Question] | None = None) -> Attempt:
    """Mark every answer, then roll the totals up onto the attempt."""
    if questions is None:
        questions = attempt.exam.questions if attempt.exam else []

    for answer in attempt.answers:
        mark_answer(answer)

    earned = sum((a.points_awarded or 0.0) for a in attempt.answers)
    pending = sum(1 for a in attempt.answers if a.needs_manual)

    # The denominator is the exam as set, not merely what the student answered,
    # so an unanswered question counts against the score.
    total = 0.0
    for question in questions:
        total += question.effective_points

    attempt.score = round(earned, 4)
    attempt.total_points = round(total, 4)
    attempt.manual_pending = pending
    return attempt


def rescore_exam(exam) -> int:
    """Re-mark every submitted attempt, preserving instructor overrides."""
    count = 0
    questions = list(exam.questions)
    for attempt in Attempt.query.filter_by(exam_id=exam.id).all():
        mark_attempt(attempt, questions)
        count += 1
    return count


def grade_essay(answer: Answer, points: float, feedback: str, grader) -> None:
    answer.points_awarded = max(0.0, min(float(points), answer.max_points or float(points)))
    answer.feedback = feedback or ""
    answer.needs_manual = False
    answer.is_correct = None
    answer.graded_by_id = getattr(grader, "id", None)
    answer.graded_at = utcnow()
