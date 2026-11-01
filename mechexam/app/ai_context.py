"""What the assistant pane is allowed to know about the page you are on.

The pane is a Word-style side panel: it opens beside whatever you are already
looking at and can see it. That is the whole point of it, and it is also the
whole risk, so the rules are in one file rather than spread through the views.

Three rules, and they are enforced here rather than trusted to the caller:

* **Ownership is re-checked, every time.** The page you are on proved you may
  see it; the identifier the browser sends back proves nothing. Every builder
  re-derives the subject and tests it against ``visible_courses``, so an
  instructor cannot read another instructor's paper by editing a number in a
  request, and a student cannot use the pane at all.

* **Names do not travel.** Marking and gradebook context go out as "the
  student". The model does not need a name to judge an answer against a
  rubric, the lecturer already knows whose paper is on the screen, and a name
  is the one thing in a transcript that turns a page of marks into a record
  about a person. Student numbers are left out for the same reason.

* **Answer keys travel only where the lecturer already has them.** An exam
  under preparation goes out with its key, because that is what checking a
  paper means. A paper being marked goes out with the key too, for the same
  reason. Neither is ever reachable by a student, because the pane is staff
  only.

Nothing here writes. These functions read and return text.
"""
from __future__ import annotations

from flask_login import current_user

from .models import (
    Attempt,
    Course,
    Exam,
    Q_ESSAY,
    Q_MULTIPART,
    Question,
    QuestionBank,
)
from .security import visible_courses

# Enough of a paper for the model to judge it, short enough that a long
# examination does not become a wall of tokens.
MAX_QUESTIONS = 40
MAX_CHARS = 14000


def _may_see(course: Course | None) -> bool:
    if course is None:
        return False
    return visible_courses(all_terms=True).filter(
        Course.id == course.id
    ).first() is not None


def _clip(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + "\n\n[…the rest was left out for length.]"


def _describe_question(question: Question, number: int, key: bool) -> str:
    lines = [f"{number}. [{question.qtype}, {question.points or 0:g} point(s)] "
             f"{(question.text or '').strip()}"]

    for index, option in enumerate(question.options or []):
        lines.append(f"     {chr(97 + index)}) {option}")

    for part in sorted(question.parts, key=lambda p: (p.order_index or 0, p.id)):
        lines.append(f"     ({part.label}) {(part.text or '').strip()}"
                     f"  [{part.points or 0:g} pt]")
        if key and part.answer:
            lines.append(f"         key: {part.answer}")

    if key and question.answer:
        unit = f" {question.unit}" if question.unit else ""
        tolerance = (f" (±{question.tolerance_percent:g}%)"
                     if question.tolerance_percent else "")
        lines.append(f"     key: {question.answer}{unit}{tolerance}")
    if key and question.explanation:
        lines.append(f"     the lecturer's note: {question.explanation.strip()}")

    return "\n".join(lines)


def _rubric_text(question: Question) -> str:
    rubric = question.rubric
    if rubric is None:
        return ""
    lines = [f"Rubric — {rubric.name}:"]
    for criterion in sorted(rubric.criteria,
                            key=lambda c: (c.order_index or 0, c.id)):
        lines.append(f"  • {criterion.title} "
                     f"(up to {criterion.max_points:g}): "
                     f"{(criterion.description or '').strip()}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The builders, one per kind of page
# --------------------------------------------------------------------------- #

def _exam_context(ident: int) -> tuple[str, str] | None:
    exam = Exam.query.get(ident)
    if exam is None or not _may_see(exam.course):
        return None

    questions = sorted(exam.questions,
                       key=lambda q: (q.order_index or 0, q.id))
    total = sum(q.points or 0 for q in questions)

    head = (f"{exam.course.code} — {exam.title}\n"
            f"{len(questions)} question(s), {total:g} point(s), "
            f"{exam.duration_minutes} minutes.")
    if exam.instructions:
        head += f"\nInstructions to students: {exam.instructions.strip()}"

    body = [head, ""]
    for number, question in enumerate(questions[:MAX_QUESTIONS], start=1):
        body.append(_describe_question(question, number, key=True))
    if len(questions) > MAX_QUESTIONS:
        body.append(f"[…and {len(questions) - MAX_QUESTIONS} more questions.]")

    return f"the paper “{exam.title}”", _clip("\n".join(body))


def _question_context(ident: int) -> tuple[str, str] | None:
    question = Question.query.get(ident)
    if question is None:
        return None
    course = None
    if question.exam is not None:
        course = question.exam.course
    elif question.bank_id:
        bank = QuestionBank.query.get(question.bank_id)
        course = bank.course if bank is not None else None
    if not _may_see(course):
        return None

    parts = [_describe_question(question, 1, key=True)]
    rubric = _rubric_text(question)
    if rubric:
        parts.append("")
        parts.append(rubric)
    return "this question", _clip("\n".join(parts))


def _attempt_context(ident: int) -> tuple[str, str] | None:
    """One student's paper, for marking. Deliberately anonymous."""
    attempt = Attempt.query.get(ident)
    if attempt is None or attempt.exam is None:
        return None
    if not _may_see(attempt.exam.course):
        return None

    lines = [f"A student's paper for {attempt.exam.course.code} — "
             f"{attempt.exam.title}.",
             "The student is not named here; you are judging the work.", ""]

    answers = {}
    for answer in attempt.answers:
        answers.setdefault(answer.question_id, []).append(answer)

    questions = sorted(attempt.exam.questions,
                       key=lambda q: (q.order_index or 0, q.id))
    shown = 0
    for number, question in enumerate(questions, start=1):
        given = answers.get(question.id) or []
        # Marking help is wanted for what a machine could not mark.
        if question.qtype not in (Q_ESSAY, Q_MULTIPART) and not any(
            a.needs_manual for a in given
        ):
            continue
        shown += 1
        if shown > MAX_QUESTIONS:
            break

        lines.append(_describe_question(question, number, key=True))
        rubric = _rubric_text(question)
        if rubric:
            lines.append(f"     {rubric}")
        for answer in given:
            label = (f" ({answer.sub_question.label})"
                     if answer.sub_question is not None else "")
            lines.append(f"     the student wrote{label}: "
                         f"{(answer.response or '').strip() or '[nothing]'}")
            if answer.points_awarded is not None and answer.graded_at:
                lines.append(f"     already given: "
                             f"{answer.points_awarded:g} of "
                             f"{answer.max_points or 0:g}")
        lines.append("")

    if shown == 0:
        lines.append("Nothing on this paper needs judgement — every item was "
                     "marked automatically.")

    return "this student's paper", _clip("\n".join(lines))


def _course_context(ident: int) -> tuple[str, str] | None:
    """How a class is doing, in aggregate. No student is named or numbered."""
    from .grading import compute_class

    course = Course.query.get(ident)
    if not _may_see(course):
        return None

    try:
        rows = compute_class(course)
    except Exception:                                       # noqa: BLE001
        rows = []

    finals = [r.final_percent for r in rows if r.final_percent is not None]

    lines = [
        f"{course.code} — {course.title}",
        f"{course.school_year}, {course.semester}. "
        f"{len(rows)} student(s) enrolled.",
        f"Lecture is {course.lecture_share:g}% of the final grade and "
        f"laboratory {course.laboratory_share:g}%."
        if course.has_laboratory else
        f"Lecture only; {course.lecture_share:g}% weighting.",
        "",
    ]

    if finals:
        graded = [r for r in rows if r.final_percent is not None]
        passing = sum(1 for r in graded if r.is_passing)
        removal = sum(1 for r in graded if r.qualifies_for_removal)
        lines += [
            f"Of {len(graded)} computed result(s): {passing} passing, "
            f"{removal} qualifying for a removal examination, "
            f"{len(graded) - passing - removal} failing outright.",
            f"Final percentage — best {max(finals):.2f}, "
            f"worst {min(finals):.2f}, "
            f"average {sum(finals) / len(finals):.2f}.",
        ]
    else:
        lines.append("No final grades are computed yet.")

    lines += ["", "Components and what has been recorded against them:"]
    for component in course.components:
        items = list(component.items)
        lines.append(f"  • {component.scope} / {component.name} "
                     f"({component.weight_percent:g}%): {len(items)} item(s)"
                     + (": " + ", ".join(
                         f"{i.title} (out of {i.max_points:g})"
                         for i in items[:12]) if items else ""))

    return f"the class record for {course.code}", _clip("\n".join(lines))


BUILDERS = {
    "exam": _exam_context,
    "question": _question_context,
    "attempt": _attempt_context,
    "course": _course_context,
}


def gather(kind: str, ident) -> tuple[str, str]:
    """Return (what the pane is looking at, the text to send). Never raises.

    An unknown kind, a missing row, or a subject this user may not see all
    give the same empty answer, so the pane cannot be used to find out what
    exists.
    """
    builder = BUILDERS.get((kind or "").strip())
    if builder is None:
        return "", ""
    try:
        ident = int(ident)
    except (TypeError, ValueError):
        return "", ""
    try:
        result = builder(ident)
    except Exception:                                       # noqa: BLE001
        return "", ""
    return result if result else ("", "")


# --------------------------------------------------------------------------- #
# What the pane offers to do
# --------------------------------------------------------------------------- #
#
# Word puts a few plain-language actions in the pane rather than leaving the
# box empty, because an empty box is the hardest thing to start with. These
# are the ones worth having here, per kind of page.

ACTIONS = {
    "exam": [
        ("Check this paper", "Check this paper for me: wrong keys, "
                             "ambiguous wording, items that give another item "
                             "away, and anything a student could reasonably "
                             "read two ways. Work the numerical items out "
                             "yourself and tell me if a key is wrong."),
        ("Is the spread right?", "Look at the coverage and difficulty of this "
                                 "paper as a whole. What is over-tested, what "
                                 "is missing, and is it doable in the time?"),
        ("Draft a few more items", "Draft three more items in the same style "
                                   "and difficulty as this paper, on topics it "
                                   "does not already cover."),
        ("Write the instructions", "Draft the instructions to students for "
                                   "this paper — what to bring, how to show "
                                   "work, how answers are marked."),
    ],
    "question": [
        ("Check this item", "Check this one item: is the key right, is the "
                            "wording unambiguous, and do the distractors all "
                            "look plausible? Work it out yourself."),
        ("Make it harder", "Rewrite this item so it tests understanding rather "
                           "than recall, keeping the same topic and point "
                           "value."),
        ("Better distractors", "Suggest better wrong options for this item — "
                               "each one should correspond to a mistake a "
                               "student actually makes, and say which mistake."),
        ("Write a worked solution", "Write the full worked solution to this "
                                    "item, step by step, in the way I would "
                                    "show it on the board."),
    ],
    "attempt": [
        ("Suggest marks and feedback", "For each item on this paper, suggest a "
                                       "mark against the rubric and two "
                                       "sentences of feedback I could give. "
                                       "Say where you are unsure."),
        ("Where did they go wrong?", "Diagnose this paper: what has the "
                                     "student actually misunderstood, as "
                                     "opposed to slipped on?"),
        ("Is my rubric covering this?", "Does the rubric cover what this "
                                        "student actually did? Tell me what "
                                        "the rubric has no room for."),
    ],
    "course": [
        ("How is this class doing?", "Read this class record and tell me what "
                                     "it says — where the class is weak, and "
                                     "whether any component is carrying too "
                                     "much weight."),
        ("Who needs help, and with what?", "From these figures, what should I "
                                           "do differently for the rest of the "
                                           "term? Be specific about topics."),
        ("Draft a remark", "Draft the kind of remark I could put on a report "
                           "for a student in the middle of this range."),
    ],
    "": [
        ("Draft exam questions", "Draft exam questions for me. Ask me what "
                                 "topic and how many first."),
        ("Explain something", "Explain a concept to me the way I could explain "
                              "it to a second-year BSME class."),
    ],
}


def actions_for(kind: str) -> list[dict]:
    return [{"label": label, "prompt": prompt}
            for label, prompt in ACTIONS.get((kind or "").strip(),
                                             ACTIONS[""])]


PANE_SYSTEM = """\
You are an assistant pane inside the examination portal of the Department of \
Mechanical Engineering, Western Philippines University. The person reading you \
is the lecturer, and they are looking at the page whose contents you have been \
given.

You suggest; you never save. Nothing you write reaches a student, a mark or a \
record unless the lecturer copies it there themselves, so give them something \
worth copying rather than something safe.

Be correct engineering first. Use SI units. Work numerical problems out \
properly and give the value you actually computed — if you cannot, say so \
rather than producing a plausible number. If the lecturer's own key or mark \
looks wrong to you, say that plainly and show the working; that is the single \
most useful thing you can do here.

Mathematics goes in LaTeX between single dollar signs, like $pV = mRT$, \
because the pane renders it that way.

Keep it short enough to read in a side panel. No preamble, no restating the \
question, no closing offer of further help.

When you are judging a student's work, remember you are seeing it without a \
name, and that the mark is the lecturer's to set. Suggest a figure and say how \
confident you are in it.
"""
