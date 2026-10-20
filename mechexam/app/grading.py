"""The grading engine.

This mirrors the College of Engineering and Technology grading sheet:

    Lecture        Written Works 20%   Performance Task 40%   Major Exams 40%
    Laboratory     Written Works 20%   Performance Task 60%   Major Exams 20%
    Final grade    67% Lecture + 33% Laboratory   (lecture only when there is
                                                   no laboratory component)

Transmutation follows the college table, and a final standing between the
removal band (55.00–59.99 by default) is *not* a failure -- it is a candidate
for the removal examination, resolved only when the instructor finalises the
subject.

Every number this module returns is derived; nothing is stored until
``finalise_course`` freezes it into ``FinalGrade``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .extensions import db
from .models import (
    Course,
    FinalGrade,
    GradeComponent,
    GradeItem,
    Score,
    SCOPE_LABORATORY,
    SCOPE_LECTURE,
    Setting,
    User,
    utcnow,
)

# --------------------------------------------------------------------------- #
# Transmutation
# --------------------------------------------------------------------------- #

TRANSMUTATION: tuple[tuple[float, str], ...] = (
    (95.60, "1.00"),
    (91.15, "1.25"),
    (86.70, "1.50"),
    (82.25, "1.75"),
    (77.80, "2.00"),
    (73.35, "2.25"),
    (68.90, "2.50"),
    (64.45, "2.75"),
    (60.00, "3.00"),
)

FAILING_GRADE = "5.00"
PASSING_GRADE = "3.00"
PASSING_PERCENT = 60.0
INCOMPLETE_GRADE = "INC"

PASSING_LADDER: tuple[str, ...] = (
    "1.00", "1.25", "1.50", "1.75", "2.00", "2.25", "2.50", "2.75", "3.00",
)

REMARK_PASSED = "Passed"
REMARK_FAILED = "Failed"
REMARK_REMOVAL = "Removal"
REMARK_INCOMPLETE = "Incomplete"


def transmute(percent: float | None) -> str:
    """Percentage -> college grade string."""
    if percent is None:
        return INCOMPLETE_GRADE
    value = round(float(percent) + 1e-9, 2)
    for threshold, grade in TRANSMUTATION:
        if value >= threshold:
            return grade
    return FAILING_GRADE


def bump_one_step(grade: str) -> str:
    """Move a passing grade one step higher (2.50 -> 2.25).

    Used when a removal examination is held: students who already passed are
    adjusted to the next higher grade.
    """
    if grade in PASSING_LADDER:
        index = PASSING_LADDER.index(grade)
        return PASSING_LADDER[max(0, index - 1)]
    return grade


def is_passing(grade: str) -> bool:
    return grade in PASSING_LADDER


# --------------------------------------------------------------------------- #
# Computation results
# --------------------------------------------------------------------------- #

@dataclass
class ItemResult:
    item: GradeItem
    points: float | None
    max_points: float
    percent: float | None
    is_override: bool = False
    remarks: str = ""

    @property
    def display(self) -> str:
        if self.points is None:
            return "—"
        text = f"{self.points:g}"
        return f"{text}*" if self.is_override else text


@dataclass
class ComponentResult:
    component: GradeComponent
    entries: list[ItemResult] = field(default_factory=list)
    raw_percent: float | None = None
    weighted: float = 0.0
    earned: float = 0.0
    possible: float = 0.0
    counted_items: int = 0

    @property
    def name(self) -> str:
        return self.component.name

    @property
    def weight(self) -> float:
        return self.component.weight_percent or 0.0


@dataclass
class ScopeResult:
    scope: str
    components: list[ComponentResult] = field(default_factory=list)
    percent: float | None = None
    weight_total: float = 0.0

    @property
    def label(self) -> str:
        return "Laboratory" if self.scope == SCOPE_LABORATORY else "Lecture"

    @property
    def has_data(self) -> bool:
        return any(c.counted_items for c in self.components)


@dataclass
class StudentResult:
    student: User
    lecture: ScopeResult
    laboratory: ScopeResult | None
    final_percent: float | None
    grade: str
    remark: str
    qualifies_for_removal: bool = False
    frozen: FinalGrade | None = None

    @property
    def is_passing(self) -> bool:
        return is_passing(self.grade)

    @property
    def completion_grade(self) -> str:
        """Only a finalised subject can carry one; otherwise the column is
        left blank for the registrar to write in by hand."""
        return (self.frozen.completion_grade or "") if self.frozen else ""

    @property
    def final_display(self) -> str:
        if self.final_percent is None:
            return "—"
        return f"{self.final_percent:.2f}"

    @property
    def effective_grade(self) -> str:
        """The grade of record: the frozen one once the subject is finalised."""
        if self.frozen is not None and self.frozen.grade:
            return self.frozen.grade
        return self.grade

    @property
    def effective_remark(self) -> str:
        if self.frozen is not None and self.frozen.remark:
            return self.frozen.remark
        return self.remark


# --------------------------------------------------------------------------- #
# Core arithmetic
# --------------------------------------------------------------------------- #

def _score_map(course: Course) -> dict[tuple[int, int], Score]:
    """All scores for a subject in one query, keyed by (item_id, student_id)."""
    item_ids = [
        item.id
        for component in course.components
        for item in component.items
    ]
    if not item_ids:
        return {}
    rows = Score.query.filter(Score.item_id.in_(item_ids)).all()
    return {(row.item_id, row.student_id): row for row in rows}


def compute_component(
    component: GradeComponent,
    student_id: int,
    scores: dict,
    method: str,
    section_id: int | None = None,
) -> ComponentResult:
    """One component's percentage for one student.

    ``total_points``  -- sum of points / sum of possible points (the default,
                         and what the college sheet does).
    ``item_average``  -- mean of each item's own percentage, so a 10-point
                         quiz counts as much as a 100-point exam.

    ``weight_multiplier`` lets a single item count more than once under
    either method.
    """
    result = ComponentResult(component=component)
    percentages: list[tuple[float, float]] = []

    for item in component.items:
        if item.section_id and section_id and item.section_id != section_id:
            continue

        # The removal examination is scored on its own and decides the outcome
        # for candidates; averaging it in would raise their standing out of the
        # removal band and so remove the very candidacy it is meant to resolve.
        if item.item_type == "removal":
            continue

        row = scores.get((item.id, student_id))
        points = row.points if row is not None else None
        max_points = item.max_points or 0.0

        # A multiplier of exactly zero means "do not count this", so it must
        # not be confused with "no multiplier set" -- `or 1.0` would.
        multiplier = item.weight_multiplier
        if multiplier is None:
            multiplier = 1.0
        if multiplier == 0:
            continue

        percent = None
        if points is not None and max_points > 0:
            percent = round(points / max_points * 100.0, 2)

        result.entries.append(
            ItemResult(
                item=item,
                points=points,
                max_points=max_points,
                percent=percent,
                is_override=bool(row.is_override) if row is not None else False,
                remarks=(row.remarks or "") if row is not None else "",
            )
        )

        # Un-entered scores are ignored rather than counted as zero, so a
        # gradebook in progress shows an honest running percentage.
        if points is None or max_points <= 0:
            continue

        result.counted_items += 1
        result.earned += points * multiplier
        result.possible += max_points * multiplier
        percentages.append((percent or 0.0, multiplier))

    if result.counted_items:
        if method == "item_average":
            weight_sum = sum(m for _, m in percentages)
            if weight_sum:
                result.raw_percent = round(
                    sum(p * m for p, m in percentages) / weight_sum, 2
                )
        else:
            if result.possible > 0:
                result.raw_percent = round(result.earned / result.possible * 100.0, 2)

    if result.raw_percent is not None:
        result.weighted = round(result.raw_percent * (component.weight_percent or 0.0) / 100.0, 4)

    return result


def compute_scope(
    course: Course,
    scope: str,
    student_id: int,
    scores: dict,
    section_id: int | None = None,
) -> ScopeResult:
    method = course.averaging_method or "total_points"
    result = ScopeResult(scope=scope)

    components = [c for c in course.components if c.scope == scope]
    components.sort(key=lambda c: (c.order_index or 0, c.id))

    weight_with_data = 0.0
    for component in components:
        computed = compute_component(component, student_id, scores, method, section_id)
        result.components.append(computed)
        result.weight_total += component.weight_percent or 0.0
        if computed.raw_percent is not None:
            weight_with_data += component.weight_percent or 0.0

    if weight_with_data > 0:
        total = sum(c.weighted for c in result.components if c.raw_percent is not None)
        # Rescale to the weight that actually has data, so a subject graded
        # halfway through still reads as a percentage out of 100.
        result.percent = round(total / weight_with_data * 100.0, 2)

    return result


def compute_student(
    course: Course,
    student: User,
    scores: dict | None = None,
    section_id: int | None = None,
    frozen_map: dict | None = None,
) -> StudentResult:
    scores = scores if scores is not None else _score_map(course)

    lecture = compute_scope(course, SCOPE_LECTURE, student.id, scores, section_id)
    laboratory = None
    if course.has_laboratory:
        laboratory = compute_scope(course, SCOPE_LABORATORY, student.id, scores, section_id)

    final_percent: float | None
    if laboratory is not None and laboratory.percent is not None and lecture.percent is not None:
        lec_share = (course.lecture_share or 67.0) / 100.0
        lab_share = (course.laboratory_share or 33.0) / 100.0
        divisor = lec_share + lab_share
        final_percent = round(
            (lecture.percent * lec_share + laboratory.percent * lab_share) / divisor, 2
        ) if divisor else None
    elif lecture.percent is not None:
        final_percent = lecture.percent
    elif laboratory is not None and laboratory.percent is not None:
        final_percent = laboratory.percent
    else:
        final_percent = None

    grade = transmute(final_percent)
    low = course.removal_low if course.removal_low is not None else 55.0
    high = course.removal_high if course.removal_high is not None else 59.99

    qualifies = (
        final_percent is not None
        and grade == FAILING_GRADE
        and low <= final_percent <= high
    )

    if final_percent is None:
        remark = REMARK_INCOMPLETE
    elif qualifies:
        remark = REMARK_REMOVAL
    elif is_passing(grade):
        remark = REMARK_PASSED
    else:
        remark = REMARK_FAILED

    frozen = None
    if frozen_map is not None:
        frozen = frozen_map.get(student.id)
    elif course.is_finalised:
        frozen = FinalGrade.query.filter_by(course_id=course.id, student_id=student.id).first()

    return StudentResult(
        student=student,
        lecture=lecture,
        laboratory=laboratory,
        final_percent=final_percent,
        grade=grade,
        remark=remark,
        qualifies_for_removal=qualifies,
        frozen=frozen,
    )


def compute_class(
    course: Course,
    section_id: int | None = None,
    students: list[User] | None = None,
) -> list[StudentResult]:
    roster = students if students is not None else course.roster(section_id)
    scores = _score_map(course)
    frozen_map = {
        row.student_id: row
        for row in FinalGrade.query.filter_by(course_id=course.id).all()
    }
    return [
        compute_student(course, student, scores, section_id, frozen_map)
        for student in roster
    ]


# --------------------------------------------------------------------------- #
# Removal examination
# --------------------------------------------------------------------------- #

def removal_item(course: Course) -> GradeItem | None:
    """The subject's removal-examination item, if the instructor made one."""
    for component in course.components:
        for item in component.items:
            if item.item_type == "removal":
                return item
    return None


def removal_candidates(course: Course, section_id: int | None = None) -> list[StudentResult]:
    return [r for r in compute_class(course, section_id) if r.qualifies_for_removal]


def finalise_course(course: Course, actor: User) -> dict:
    """Freeze grades and resolve the removal examination.

    The rules the instructor asked for:

    *   A student in the removal band who passes the removal examination gets
        exactly 3.00 -- not a transmuted score.
    *   Holding a removal examination adjusts everyone who already passed one
        grade step higher (2.50 becomes 2.25).
    *   The whole thing is reversible: ``reopen_course`` undoes it.
    """
    results = compute_class(course)
    item = removal_item(course)
    removal_scores: dict[int, Score] = {}
    if item is not None:
        removal_scores = {
            row.student_id: row
            for row in Score.query.filter_by(item_id=item.id).all()
        }

    held_removal = bool(item is not None and removal_scores)
    summary = {"passed": 0, "failed": 0, "removal_passed": 0,
               "removal_failed": 0, "bumped": 0, "incomplete": 0}

    pass_mark = Setting.get_float("passing_percent", PASSING_PERCENT)

    for result in results:
        row = FinalGrade.query.filter_by(
            course_id=course.id, student_id=result.student.id
        ).first()
        if row is None:
            row = FinalGrade(course_id=course.id, student_id=result.student.id)
            db.session.add(row)

        row.lecture_percent = result.lecture.percent
        row.laboratory_percent = result.laboratory.percent if result.laboratory else None
        row.final_percent = result.final_percent
        row.pre_removal_grade = result.grade
        row.was_bumped = False
        row.removal_status = ""
        row.removal_percent = None
        row.finalised_at = utcnow()

        grade = result.grade
        remark = result.remark

        if result.final_percent is None:
            row.grade = INCOMPLETE_GRADE
            row.remark = REMARK_INCOMPLETE
            summary["incomplete"] += 1
            continue

        if result.qualifies_for_removal:
            score_row = removal_scores.get(result.student.id)
            if score_row is not None and score_row.points is not None and item is not None:
                max_points = item.max_points or 0.0
                percent = (score_row.points / max_points * 100.0) if max_points else 0.0
                row.removal_percent = round(percent, 2)
                if percent >= pass_mark:
                    grade = PASSING_GRADE          # exactly 3.00, by rule
                    remark = REMARK_PASSED
                    row.removal_status = "passed"
                    summary["removal_passed"] += 1
                else:
                    grade = FAILING_GRADE
                    remark = REMARK_FAILED
                    row.removal_status = "failed"
                    summary["removal_failed"] += 1
            else:
                # Qualified but did not sit the examination.
                grade = FAILING_GRADE
                remark = REMARK_REMOVAL
                row.removal_status = "qualified"
                summary["removal_failed"] += 1
        elif held_removal and is_passing(grade):
            bumped = bump_one_step(grade)
            if bumped != grade:
                row.was_bumped = True
                summary["bumped"] += 1
            grade = bumped
            remark = REMARK_PASSED

        row.grade = grade
        row.remark = remark
        if is_passing(grade):
            summary["passed"] += 1
        elif grade == FAILING_GRADE and row.removal_status not in ("failed", "qualified"):
            summary["failed"] += 1
        elif grade == FAILING_GRADE and row.removal_status == "failed":
            pass  # already counted
        elif grade == FAILING_GRADE:
            summary["failed"] += 1

    course.is_finalised = True
    course.finalised_at = utcnow()
    db.session.commit()
    summary["held_removal"] = held_removal
    return summary


def reopen_course(course: Course) -> None:
    """Undo finalisation; the computed grades go back to being live."""
    FinalGrade.query.filter_by(course_id=course.id).delete(synchronize_session=False)
    course.is_finalised = False
    course.finalised_at = None
    db.session.commit()


# --------------------------------------------------------------------------- #
# Setup helpers
# --------------------------------------------------------------------------- #

def ensure_components(course: Course) -> None:
    """Give a new subject the CET default components."""
    existing = {(c.scope, c.name) for c in course.components}
    order = 0
    scopes = [SCOPE_LECTURE]
    if course.has_laboratory:
        scopes.append(SCOPE_LABORATORY)

    for scope in scopes:
        for name, weight, kind in GradeComponent.DEFAULTS[scope]:
            order += 1
            if (scope, name) in existing:
                continue
            db.session.add(
                GradeComponent(
                    course_id=course.id,
                    name=name,
                    scope=scope,
                    weight_percent=weight,
                    kind=kind,
                    order_index=order,
                )
            )
    db.session.commit()


def class_tally(results: list[StudentResult]) -> dict:
    """Pass / removal / fail counts for the foot of the printed grade sheet."""
    tally = {"total": len(results), "passed": 0, "removal": 0,
             "failed": 0, "incomplete": 0}
    for result in results:
        remark = result.effective_remark
        if remark == REMARK_PASSED:
            tally["passed"] += 1
        elif remark == REMARK_REMOVAL:
            tally["removal"] += 1
        elif remark == REMARK_INCOMPLETE:
            tally["incomplete"] += 1
        else:
            tally["failed"] += 1
    return tally
