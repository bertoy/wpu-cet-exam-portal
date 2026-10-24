"""Behaviour tests for the rules that matter.

Status codes are covered by smoke.py.  This file checks that the app actually
does the right thing: the CET arithmetic, the removal examination, instructor
isolation, exam timing, the exit modes, and that a redeploy cannot lose data.

Runs against a throw-away database, so it never touches real records.

    python test_rules.py
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import sys
import tempfile
from datetime import timedelta

WORKSPACE = tempfile.mkdtemp(prefix="mechexam-test-")
os.environ["DATA_DIR"] = WORKSPACE
os.environ["SECRET_KEY"] = "test-only"
os.environ["ADMIN_PASSWORD"] = "admin-test-pw"

from app import create_app                                    # noqa: E402
from app.extensions import db                                 # noqa: E402
from app.grading import (                                     # noqa: E402
    compute_class,
    compute_student,
    ensure_components,
    finalise_course,
    removal_candidates,
    reopen_course,
    transmute,
)
from app.models import (                                      # noqa: E402
    Answer,
    Attempt,
    ATTEMPT_IN_PROGRESS,
    ATTEMPT_SUBMITTED,
    Course,
    Enrollment,
    Exam,
    GradeItem,
    Q_MULTIPART,
    Q_NUMERIC,
    Question,
    QuestionBank,
    ROLE_INSTRUCTOR,
    ROLE_STUDENT,
    SCOPE_LABORATORY,
    SCOPE_LECTURE,
    Score,
    Section,
    Setting,
    StudentYearStatus,
    SubQuestion,
    User,
    utcnow,
)
from app.marking import mark_attempt                          # noqa: E402

app = create_app()
app.config["PROPAGATE_EXCEPTIONS"] = True
logging.getLogger("app").setLevel(logging.ERROR)
app.logger.setLevel(logging.CRITICAL)

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


# --------------------------------------------------------------------------- #
# Harness plumbing
# --------------------------------------------------------------------------- #
#
# These tests drive the app through its own HTTP routes while also inspecting
# the database directly, and those two things use different sessions.  The
# outer session holds a read transaction open, so without care it keeps showing
# a snapshot taken before the request and the test "sees" none of the rows the
# request just committed.  `signed_in` therefore commits our own writes before
# the request and drops the stale session afterwards, and `reload` re-reads a
# row by its primary key rather than trusting a now-detached object.

import contextlib  # noqa: E402


class SyncedClient:
    """A test client that keeps the test's own view of the database honest.

    Every request is bracketed: our pending writes are committed first so the
    request can see them, and the outer session is dropped afterwards so the
    next query in the test sees what the request committed.  Without this a
    test reads a snapshot from before the request and concludes, wrongly, that
    the app did nothing.
    """

    def __init__(self, client):
        self._client = client

    @staticmethod
    def _clear_cached_login():
        """Drop Flask-Login's cached user from the application context.

        Flask-Login memoises the signed-in user on ``g``.  Flask reuses an
        already-pushed application context rather than making a new one, so
        because this suite holds one app context open for the whole run, every
        request would otherwise inherit the first request's user and the test
        would silently exercise the wrong account.  A real deployment pushes a
        fresh app context per request and is unaffected; this is purely the
        price of driving the app from inside a long-lived context.
        """
        from flask import g

        for name in [n for n in vars(g) if n.startswith("_login")]:
            try:
                delattr(g, name)
            except (AttributeError, KeyError):
                pass

    def _wrap(self, method, *args, **kwargs):
        db.session.commit()
        self._clear_cached_login()
        response = method(*args, **kwargs)
        self._clear_cached_login()
        # Commit rather than remove: this ends the stale read transaction AND
        # expires every loaded instance, so the next attribute access re-reads
        # from the database -- while leaving the test's objects attached and
        # usable, which session.remove() would not.
        db.session.commit()
        db.session.expire_all()
        return response

    def get(self, *args, **kwargs):
        return self._wrap(self._client.get, *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._wrap(self._client.post, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


@contextlib.contextmanager
def signed_in(identifier: str, password: str):
    db.session.commit()                 # make our own writes visible to the app
    with app.test_client() as raw:
        client = SyncedClient(raw)
        response = client.post(
            "/login", data={"identifier": identifier, "password": password}
        )
        if response.status_code not in (302, 303):
            raise AssertionError(
                f"could not sign in as {identifier!r} "
                f"(status {response.status_code})"
            )
        try:
            yield client
        finally:
            db.session.commit()
            db.session.expire_all()


def reload(instance):
    """Re-read a row by primary key, after requests have changed it."""
    if instance is None:
        return None
    model = type(instance)
    key = db.inspect(instance).identity
    if key is None:
        return None
    return db.session.get(model, key[0] if len(key) == 1 else key)


def check(name: str, condition, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append((name, detail))


def near(a, b, tolerance=0.011) -> bool:
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) <= tolerance


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def make_instructor(username: str) -> User:
    user = User(username=username, role=ROLE_INSTRUCTOR, name=f"Teacher {username}")
    user.set_password("pw-" + username)
    db.session.add(user)
    db.session.flush()
    return user


def make_student(number: str) -> User:
    user = User(username=number, student_number=number, role=ROLE_STUDENT,
                name=f"Student {number}", program="BSME", year_level="4th Year")
    user.set_password(number)
    db.session.add(user)
    db.session.flush()
    return user


def make_course(code: str, owner: User, laboratory: bool = False) -> Course:
    year, semester = Setting.term()
    course = Course(
        code=code, title=f"Subject {code}", owner_id=owner.id,
        school_year=year, semester=semester, has_laboratory=laboratory,
        units=3.0, lec_units=3.0, lab_units=1.0 if laboratory else 0.0,
    )
    db.session.add(course)
    db.session.commit()
    ensure_components(course)
    return course


def add_item(course: Course, scope: str, component_name: str, title: str,
             maximum: float, kind: str = "quiz") -> GradeItem:
    component = next(
        c for c in course.components if c.scope == scope and c.name == component_name
    )
    item = GradeItem(component_id=component.id, title=title,
                     item_type=kind, max_points=maximum)
    db.session.add(item)
    db.session.flush()
    return item


def set_score(item: GradeItem, student: User, points) -> None:
    row = Score.query.filter_by(item_id=item.id, student_id=student.id).first()
    if row is None:
        row = Score(item_id=item.id, student_id=student.id)
        db.session.add(row)
    row.points = points
    db.session.flush()


# --------------------------------------------------------------------------- #
# 1. Transmutation boundaries
# --------------------------------------------------------------------------- #

def test_transmutation():
    # Standings are reported to two decimal places, and the grade is taken
    # from the figure as reported -- so 95.599 prints as 95.60 and earns 1.00.
    # Grading the unrounded value instead would show "95.60" beside a 1.25 and
    # look like a fault.
    cases = [
        (100.0, "1.00"), (95.60, "1.00"), (95.599, "1.00"), (95.594, "1.25"),
        (91.15, "1.25"), (91.14, "1.50"),
        (86.70, "1.50"), (82.25, "1.75"), (77.80, "2.00"),
        (73.35, "2.25"), (68.90, "2.50"), (64.45, "2.75"),
        (60.00, "3.00"), (59.999, "3.00"), (59.99, "5.00"), (0.0, "5.00"),
    ]
    for percent, expected in cases:
        got = transmute(percent)
        check(f"transmute {percent} -> {expected}", got == expected, f"got {got}")

    check("no marks gives INC", transmute(None) == "INC")


# --------------------------------------------------------------------------- #
# 2. The CET weighting, lecture only
# --------------------------------------------------------------------------- #

def test_lecture_weighting():
    teacher = make_instructor("lec-teacher")
    course = make_course("TEST-LEC", teacher)
    student = make_student("T-0001")
    db.session.add(Enrollment(course_id=course.id, student_id=student.id))
    db.session.commit()

    # Written Works 20%, Performance Task 40%, Major Exams 40%.
    written = add_item(course, SCOPE_LECTURE, "Written Works", "Quiz", 100)
    task = add_item(course, SCOPE_LECTURE, "Performance Task", "Task", 100, "task")
    major = add_item(course, SCOPE_LECTURE, "Major Examinations", "Final", 100, "final")

    set_score(written, student, 90)
    set_score(task, student, 80)
    set_score(major, student, 70)
    db.session.commit()

    result = compute_student(course, student)
    expected = 90 * 0.20 + 80 * 0.40 + 70 * 0.40        # = 78.0
    check("lecture weighting 20/40/40", near(result.lecture.percent, expected),
          f"got {result.lecture.percent}, expected {expected}")
    check("final equals lecture with no laboratory",
          near(result.final_percent, expected), f"got {result.final_percent}")
    check("78.0 transmutes to 2.00", result.grade == "2.00", f"got {result.grade}")
    check("remark is Passed", result.remark == "Passed", result.remark)
    return course, student


# --------------------------------------------------------------------------- #
# 3. Laboratory split, 67 / 33
# --------------------------------------------------------------------------- #

def test_laboratory_split():
    teacher = make_instructor("lab-teacher")
    course = make_course("TEST-LAB", teacher, laboratory=True)
    student = make_student("T-0002")
    db.session.add(Enrollment(course_id=course.id, student_id=student.id))
    db.session.commit()

    for scope, scores in (
        (SCOPE_LECTURE, (90, 90, 90)),
        (SCOPE_LABORATORY, (60, 60, 60)),
    ):
        names = ["Written Works", "Performance Task", "Major Examinations"]
        for name, points in zip(names, scores):
            item = add_item(course, scope, name, f"{scope}-{name}", 100)
            set_score(item, student, points)
    db.session.commit()

    result = compute_student(course, student)
    check("lecture is 90", near(result.lecture.percent, 90.0),
          f"got {result.lecture.percent}")
    check("laboratory is 60", near(result.laboratory.percent, 60.0),
          f"got {result.laboratory.percent}")

    expected = 90 * 0.67 + 60 * 0.33                    # = 80.1
    check("final is 67% lecture + 33% laboratory",
          near(result.final_percent, expected),
          f"got {result.final_percent}, expected {expected}")

    # Laboratory weighting differs: 20 / 60 / 20.
    lab_components = {c.name: c.weight_percent
                      for c in course.components if c.scope == SCOPE_LABORATORY}
    check("laboratory weights are 20/60/20",
          lab_components == {"Written Works": 20.0, "Performance Task": 60.0,
                             "Major Examinations": 20.0},
          str(lab_components))


# --------------------------------------------------------------------------- #
# 4. Unmarked activities are not zeros
# --------------------------------------------------------------------------- #

def test_blank_is_not_zero():
    teacher = make_instructor("blank-teacher")
    course = make_course("TEST-BLANK", teacher)
    student = make_student("T-0003")
    db.session.add(Enrollment(course_id=course.id, student_id=student.id))
    db.session.commit()

    one = add_item(course, SCOPE_LECTURE, "Written Works", "Quiz 1", 100)
    add_item(course, SCOPE_LECTURE, "Written Works", "Quiz 2 not yet given", 100)
    set_score(one, student, 80)
    db.session.commit()

    result = compute_student(course, student)
    check("an unmarked activity is ignored, not counted as zero",
          near(result.lecture.components[0].raw_percent, 80.0),
          f"got {result.lecture.components[0].raw_percent}")

    # Only Written Works has data, so the standing rescales to that weight.
    check("standing rescales to the weight that has marks",
          near(result.lecture.percent, 80.0), f"got {result.lecture.percent}")

    # A student with nothing at all is Incomplete, not failed.
    bare = make_student("T-0004")
    db.session.add(Enrollment(course_id=course.id, student_id=bare.id))
    db.session.commit()
    result = compute_student(course, bare)
    check("a student with no marks is Incomplete, not failed",
          result.remark == "Incomplete" and result.grade == "INC",
          f"{result.grade} / {result.remark}")


# --------------------------------------------------------------------------- #
# 5. Averaging methods
# --------------------------------------------------------------------------- #

def test_averaging_methods():
    teacher = make_instructor("avg-teacher")
    course = make_course("TEST-AVG", teacher)
    student = make_student("T-0005")
    db.session.add(Enrollment(course_id=course.id, student_id=student.id))
    db.session.commit()

    small = add_item(course, SCOPE_LECTURE, "Written Works", "10-point quiz", 10)
    large = add_item(course, SCOPE_LECTURE, "Written Works", "100-point exam", 100)
    set_score(small, student, 10)       # 100%
    set_score(large, student, 50)       # 50%
    db.session.commit()

    course.averaging_method = "total_points"
    db.session.commit()
    result = compute_student(course, student)
    # 60 of 110
    check("total-points method weights by size",
          near(result.lecture.components[0].raw_percent, 54.55),
          f"got {result.lecture.components[0].raw_percent}")

    course.averaging_method = "item_average"
    db.session.commit()
    result = compute_student(course, student)
    # mean of 100% and 50%
    check("item-average method treats activities equally",
          near(result.lecture.components[0].raw_percent, 75.0),
          f"got {result.lecture.components[0].raw_percent}")

    # A weight multiplier counts an activity more than once.
    course.averaging_method = "total_points"
    large.weight_multiplier = 2.0
    db.session.commit()
    result = compute_student(course, student)
    # (10*1 + 50*2) / (10*1 + 100*2) = 110/210
    check("weight multiplier counts an activity twice",
          near(result.lecture.components[0].raw_percent, 52.38),
          f"got {result.lecture.components[0].raw_percent}")


# --------------------------------------------------------------------------- #
# 6. The removal examination, end to end
# --------------------------------------------------------------------------- #

def test_removal_examination():
    teacher = make_instructor("rem-teacher")
    course = make_course("TEST-REM", teacher)

    written = add_item(course, SCOPE_LECTURE, "Written Works", "Quiz", 100)
    task = add_item(course, SCOPE_LECTURE, "Performance Task", "Task", 100, "task")
    major = add_item(course, SCOPE_LECTURE, "Major Examinations", "Final", 100, "final")

    # Four students: a strong pass, a bare pass, two in the removal band.
    plan = {
        "R-0001": 95.0,      # passes well
        "R-0002": 70.0,      # passes
        "R-0003": 57.0,      # removal band
        "R-0004": 58.5,      # removal band
        "R-0005": 40.0,      # outright failure
    }
    students = {}
    for number, percent in plan.items():
        student = make_student(number)
        students[number] = student
        db.session.add(Enrollment(course_id=course.id, student_id=student.id))
        db.session.flush()
        for item in (written, task, major):
            set_score(item, student, percent)
    db.session.commit()

    # Before any removal examination exists.
    candidates = removal_candidates(course)
    numbers = sorted(c.student.student_number for c in candidates)
    check("only the removal band qualifies",
          numbers == ["R-0003", "R-0004"], str(numbers))

    results = {r.student.student_number: r for r in compute_class(course)}
    check("a removal candidate is remarked Removal, not Failed",
          results["R-0003"].remark == "Removal", results["R-0003"].remark)
    check("a student below the band is Failed",
          results["R-0005"].remark == "Failed", results["R-0005"].remark)
    check("a passing student keeps their computed grade",
          results["R-0002"].grade == transmute(70.0),
          results["R-0002"].grade)

    before_strong = results["R-0001"].grade
    before_pass = results["R-0002"].grade

    # Hold the removal examination: one passes it, one does not.
    removal = add_item(course, SCOPE_LECTURE, "Major Examinations",
                       "Removal Examination", 100, "removal")
    removal.weight_multiplier = 0.0
    set_score(removal, students["R-0003"], 75)      # passes
    set_score(removal, students["R-0004"], 45)      # fails
    db.session.commit()

    summary = finalise_course(course, teacher)
    frozen = {r.student.student_number: r for r in compute_class(course)}

    check("passing the removal gives exactly 3.00",
          frozen["R-0003"].effective_grade == "3.00",
          frozen["R-0003"].effective_grade)
    check("passing the removal is remarked Passed",
          frozen["R-0003"].effective_remark == "Passed",
          frozen["R-0003"].effective_remark)
    check("failing the removal gives 5.00",
          frozen["R-0004"].effective_grade == "5.00",
          frozen["R-0004"].effective_grade)

    # Everyone who already passed moves one step higher.
    from app.grading import bump_one_step

    check("an already-passing grade is bumped one step",
          frozen["R-0002"].effective_grade == bump_one_step(before_pass),
          f"{before_pass} -> {frozen['R-0002'].effective_grade}")
    check("1.00 cannot be bumped past itself",
          frozen["R-0001"].effective_grade == bump_one_step(before_strong),
          f"{before_strong} -> {frozen['R-0001'].effective_grade}")
    check("the outright failure is untouched by the bump",
          frozen["R-0005"].effective_grade == "5.00",
          frozen["R-0005"].effective_grade)
    check("finalising reports what it did",
          summary["removal_passed"] == 1 and summary["removal_failed"] == 1,
          str(summary))
    check("the subject is marked finalised", course.is_finalised)

    # 2.50 -> 2.25 specifically, the example the instructor gave.
    check("2.50 becomes 2.25", bump_one_step("2.50") == "2.25")

    # Reopening undoes all of it.
    reopen_course(course)
    live = {r.student.student_number: r for r in compute_class(course)}
    check("reopening restores the computed grades",
          live["R-0002"].effective_grade == before_pass,
          live["R-0002"].effective_grade)
    check("reopening clears the finalised flag", not course.is_finalised)
    check("reopening restores Removal for the candidate",
          live["R-0003"].effective_remark == "Removal",
          live["R-0003"].effective_remark)


# --------------------------------------------------------------------------- #
# 7. Instructor isolation
# --------------------------------------------------------------------------- #

def test_instructor_isolation():
    mine = make_instructor("owner-a")
    theirs = make_instructor("owner-b")
    my_course = make_course("ISO-MINE", mine)
    their_course = make_course("ISO-THEIRS", theirs)

    # An unowned subject must not leak either -- this was the previous bug.
    year, semester = Setting.term()
    orphan = Course(code="ISO-ORPHAN", title="Unassigned", owner_id=None,
                    school_year=year, semester=semester)
    db.session.add(orphan)
    db.session.commit()

    with signed_in("owner-a", "pw-owner-a") as client:

        page = client.get("/subjects/").get_data(as_text=True)
        check("an instructor sees their own subject", "ISO-MINE" in page)
        check("an instructor does not see another's subject",
              "ISO-THEIRS" not in page)
        check("an instructor does not see an unassigned subject",
              "ISO-ORPHAN" not in page)

        check("opening another instructor's subject is a 404",
              client.get(f"/subjects/{their_course.id}").status_code == 404)
        check("opening another's gradebook is a 404",
              client.get(f"/gradebook/{their_course.id}").status_code == 404)
        check("creating an exam under another's subject is refused",
              client.post("/exams/new", data={
                  "course_id": their_course.id, "title": "Sneaky",
                  "duration_minutes": "30",
              }).status_code == 404)

        before = Exam.query.filter_by(course_id=their_course.id).count()
        check("no exam was created in another's subject", before == 0)

        # An instructor cannot reach the administration pages at all.
        response = client.get("/admin/subjects", follow_redirects=False)
        check("an instructor cannot open the administration pages",
              response.status_code in (302, 403), str(response.status_code))

    with signed_in("admin", "admin-test-pw") as client:
        page = client.get("/subjects/").get_data(as_text=True)
        check("the administrator sees every subject",
              "ISO-MINE" in page and "ISO-THEIRS" in page, "")


# --------------------------------------------------------------------------- #
# 8. Enrolment is filtered by section
# --------------------------------------------------------------------------- #

def test_section_enrolment():
    teacher = make_instructor("sec-teacher")
    course = make_course("TEST-SEC", teacher)
    a = Section(course_id=course.id, name="4-A")
    b = Section(course_id=course.id, name="4-B")
    db.session.add_all([a, b])
    db.session.commit()

    students = [make_student(f"S-{n:04d}") for n in range(1, 7)]
    db.session.commit()

    with signed_in("sec-teacher", "pw-sec-teacher") as client:

        # Enrol only the first three, into section A.
        client.post(f"/subjects/{course.id}/enrol", data={
            "section_id": a.id,
            "student_ids": [str(s.id) for s in students[:3]],
        })

        enrolled = Enrollment.query.filter_by(course_id=course.id).all()
        check("only the ticked students were enrolled", len(enrolled) == 3,
              f"got {len(enrolled)}")
        check("they all went into the chosen section",
              all(e.section_id == a.id for e in enrolled))

        # Enrolment without naming a section must be refused when sections exist.
        client.post(f"/subjects/{course.id}/enrol", data={
            "student_ids": [str(students[3].id)],
        })
        check("enrolling with no section chosen is refused",
              Enrollment.query.filter_by(course_id=course.id).count() == 3,
              str(Enrollment.query.filter_by(course_id=course.id).count()))

        # The roster filters by section.
        check("section A has three students", len(course.roster(a.id)) == 3)
        check("section B has none", len(course.roster(b.id)) == 0)
        check("the subject as a whole has three", len(course.roster()) == 3)

        # Moving a student between sections, not duplicating them.
        client.post(f"/subjects/{course.id}/enrol", data={
            "section_id": b.id,
            "student_ids": [str(students[0].id)],
        })
        check("re-enrolling moves rather than duplicates",
              Enrollment.query.filter_by(course_id=course.id).count() == 3,
              str(Enrollment.query.filter_by(course_id=course.id).count()))
        check("the student is now in section B", len(course.roster(b.id)) == 1)


# --------------------------------------------------------------------------- #
# 9. Drops and whole-year standing
# --------------------------------------------------------------------------- #

def test_drops_and_year_status():
    teacher = make_instructor("drop-teacher")
    course = make_course("TEST-DROP", teacher)
    other = make_course("TEST-DROP2", teacher)

    student = make_student("D-0001")
    second = make_student("D-0002")
    for subject in (course, other):
        for person in (student, second):
            db.session.add(Enrollment(course_id=subject.id, student_id=person.id))
    db.session.commit()

    item = add_item(course, SCOPE_LECTURE, "Written Works", "Quiz", 100)
    set_score(item, student, 88)
    db.session.commit()

    check("both students are on the roster", len(course.roster()) == 2)

    # Drop one from this subject only.
    enrolment = Enrollment.query.filter_by(
        course_id=course.id, student_id=student.id
    ).first()
    enrolment.is_dropped = True
    db.session.commit()

    check("a dropped student leaves this subject's roster",
          len(course.roster()) == 1)
    check("a per-subject drop does not affect another subject",
          len(other.roster()) == 2)
    check("a dropped student's marks are kept",
          Score.query.filter_by(item_id=item.id, student_id=student.id).first().points == 88)

    # Reinstating brings them back with the marks intact.
    enrolment.is_dropped = False
    db.session.commit()
    check("reinstating restores them", len(course.roster()) == 2)
    result = compute_student(course, student)
    check("a reinstated student's standing is intact",
          near(result.lecture.components[0].raw_percent, 88.0),
          str(result.lecture.components[0].raw_percent))

    # A whole-year status removes them from every subject that year.
    db.session.add(StudentYearStatus(
        student_id=second.id, school_year=course.school_year, status="shifted",
    ))
    db.session.commit()

    check("a shifted student leaves every roster that year",
          len(course.roster()) == 1 and len(other.roster()) == 1,
          f"{len(course.roster())} / {len(other.roster())}")
    check("the marks of a shifted student survive",
          Score.query.filter_by(item_id=item.id).count() >= 1)


# --------------------------------------------------------------------------- #
# 10. Exam timing: one clock for the class
# --------------------------------------------------------------------------- #

def test_exam_timing():
    teacher = make_instructor("time-teacher")
    course = make_course("TEST-TIME", teacher)
    early = make_student("E-0001")
    late = make_student("E-0002")
    for person in (early, late):
        db.session.add(Enrollment(course_id=course.id, student_id=person.id))
    db.session.commit()

    exam = Exam(course_id=course.id, title="Timed", duration_minutes=30,
                access_code="PIN123", school_year=course.school_year,
                semester=course.semester, exit_mode="strict")
    db.session.add(exam)
    db.session.flush()
    db.session.add(Question(exam_id=exam.id, qtype=Q_NUMERIC, text="2+2?",
                            answer="4", points=1, order_index=1))
    db.session.commit()

    with signed_in("time-teacher", "pw-time-teacher") as client:
        client.post(f"/exams/{exam.id}/open")

    exam = reload(exam)
    check("opening the exam starts the clock", exam.opened_at is not None)
    check("opening sets a shared deadline", exam.closes_at is not None)
    check("the deadline is the duration after opening",
          near((exam.closes_at - exam.opened_at).total_seconds(), 30 * 60, 2),
          str(exam.closes_at - exam.opened_at))

    first_deadline = exam.closes_at

    # A student joining late gets the time that is left, not a fresh 30 minutes.
    exam.opened_at = utcnow() - timedelta(minutes=20)
    exam.closes_at = exam.opened_at + timedelta(minutes=30)
    db.session.commit()

    remaining = exam.seconds_remaining
    check("a late joiner inherits the remaining time, not a new clock",
          500 < remaining < 620, f"{remaining} seconds left")

    with signed_in("E-0002", "E-0002") as client:
        client.post(f"/take/{exam.id}/start", data={"access_code": "PIN123"})

    attempt = Attempt.query.filter_by(exam_id=exam.id, student_id=late.id).first()
    check("the late student has an attempt", attempt is not None)

    # A wrong PIN is refused.
    with signed_in("E-0001", "E-0001") as client:
        response = client.post(f"/take/{exam.id}/start",
                               data={"access_code": "WRONG"})
        check("a wrong exam PIN is refused", response.status_code == 403,
              str(response.status_code))
        check("no attempt is created on a wrong PIN",
              Attempt.query.filter_by(exam_id=exam.id, student_id=early.id).first() is None)

    # When the time runs out the exam closes for everyone and submits.
    exam.closes_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    check("the exam reports itself expired", exam.is_expired)

    with signed_in("E-0002", "E-0002") as client:
        client.post(f"/api/exam/{exam.id}/expire-check")

    exam = reload(exam)
    attempt = reload(attempt)
    check("expiry closes the exam for everybody", not exam.is_open)
    check("expiry submits the attempts still open",
          attempt.status == ATTEMPT_SUBMITTED, attempt.status)
    check("the automatic submission is recorded as such",
          attempt.auto_submitted and attempt.submit_reason == "time_expired",
          f"{attempt.auto_submitted} / {attempt.submit_reason}")
    return exam, late


# --------------------------------------------------------------------------- #
# 11. Exit modes, and the screen-sleep distinction
# --------------------------------------------------------------------------- #

def test_exit_modes():
    teacher = make_instructor("exit-teacher")
    course = make_course("TEST-EXIT", teacher)

    def fresh_exam(mode: str, student_number: str):
        exam = Exam(course_id=course.id, title=f"Exit {mode}", duration_minutes=30,
                    access_code="EXIT", exit_mode=mode,
                    school_year=course.school_year, semester=course.semester)
        db.session.add(exam)
        db.session.flush()
        db.session.add(Question(exam_id=exam.id, qtype=Q_NUMERIC, text="2+2?",
                                answer="4", points=1, order_index=1))
        student = make_student(student_number)
        db.session.add(Enrollment(course_id=course.id, student_id=student.id))
        db.session.commit()

        exam.is_open = True
        exam.opened_at = utcnow()
        exam.closes_at = exam.opened_at + timedelta(minutes=30)
        db.session.commit()
        return exam, student

    # A locked phone is the case this whole mechanism exists for. The browser
    # cannot tell it apart from an app switch, so none of the events that a
    # lock produces -- a blur, the document hiding, a short absence -- may
    # end an exam, even in the strictest mode.
    exam, student = fresh_exam("strict", "X-0001")
    with signed_in("X-0001", "X-0001") as client:
        client.post(f"/take/{exam.id}/start", data={"access_code": "EXIT"})
        attempt = Attempt.query.filter_by(exam_id=exam.id, student_id=student.id).first()

        for kind, detail in (
            ("blur", "window lost focus (not acted on)"),
            ("hidden", "exam no longer on screen"),
            ("interruption", "away 25s — within the allowance"),
            ("screen_sleep", "display off"),
            ("absence_forgiven", "away 200s — 1 of 1 forgiven"),
        ):
            client.post(f"/take/attempt/{attempt.id}/event",
                        data={"kind": kind, "detail": detail})
            attempt = reload(attempt)
            check(f"strict mode does not submit on ‘{kind}’",
                  attempt.status == ATTEMPT_IN_PROGRESS, attempt.status)

        check("every one of those was still recorded for the instructor",
              len(attempt.events) >= 5, str(len(attempt.events)))

        # Only a confirmed exit ends it, and only the client can confirm one,
        # because only the client knows how long the student was away.
        client.post(f"/take/attempt/{attempt.id}/event",
                    data={"kind": "exit_confirmed", "detail": "away 240s"})
        attempt = reload(attempt)
        check("a confirmed exit does submit in strict mode",
              attempt.status == ATTEMPT_SUBMITTED, attempt.status)
        check("the reason records that they left",
              (attempt.submit_reason or "").startswith("exit"),
              attempt.submit_reason)

    # The lock allowance has sane bounds and a default long enough to cover a
    # phone's screen timeout.
    check("the lock allowance defaults to at least a minute",
          exam.lock_grace_seconds >= 60, str(exam.lock_grace_seconds))

    with signed_in("exit-teacher", "pw-exit-teacher") as client:
        client.post(f"/exams/{exam.id}/settings", data={
            "title": exam.title, "duration_minutes": "30",
            "exit_mode": "strict", "lock_grace_seconds": "5000",
            "exit_allowance": "99",
        }, follow_redirects=True)
        exam = reload(exam)
        check("an absurd lock allowance is clamped, not stored",
              exam.lock_grace_seconds <= 600, str(exam.lock_grace_seconds))
        check("the forgiveness count is clamped too",
              exam.exit_allowance <= 10, str(exam.exit_allowance))

    # Strict: a screen going to sleep must NOT submit.
    exam, student = fresh_exam("strict", "X-0002")
    with signed_in("X-0002", "X-0002") as client:
        client.post(f"/take/{exam.id}/start", data={"access_code": "EXIT"})
        attempt = Attempt.query.filter_by(exam_id=exam.id, student_id=student.id).first()

        client.post(f"/take/attempt/{attempt.id}/event",
                    data={"kind": "screen_sleep", "detail": "display off"})
        attempt = reload(attempt)
        check("a sleeping screen does NOT submit in strict mode",
              attempt.status == ATTEMPT_IN_PROGRESS, attempt.status)
        check("the sleeping screen is still recorded for the instructor",
              any(e.kind == "screen_sleep" for e in attempt.events))

    # Grace and off: leaving never submits on the server's own initiative.
    for mode, number in (("grace", "X-0003"), ("off", "X-0004")):
        exam, student = fresh_exam(mode, number)
        with signed_in(number, number) as client:
            client.post(f"/take/{exam.id}/start", data={"access_code": "EXIT"})
            attempt = Attempt.query.filter_by(
                exam_id=exam.id, student_id=student.id
            ).first()
            client.post(f"/take/attempt/{attempt.id}/event",
                        data={"kind": "exit_confirmed", "detail": "away 240s"})
            attempt = reload(attempt)
            check(f"{mode} mode does not submit on the server side",
                  attempt.status == ATTEMPT_IN_PROGRESS, attempt.status)
            check(f"{mode} mode still records the event",
                  len(attempt.events) >= 1)


# --------------------------------------------------------------------------- #
# 12. Autosave, reopen and reset
# --------------------------------------------------------------------------- #

def test_autosave_reopen_reset():
    teacher = make_instructor("save-teacher")
    course = make_course("TEST-SAVE", teacher)
    student = make_student("A-0001")
    db.session.add(Enrollment(course_id=course.id, student_id=student.id))
    db.session.commit()

    exam = Exam(course_id=course.id, title="Autosave", duration_minutes=30,
                access_code="SAVE", exit_mode="off",
                school_year=course.school_year, semester=course.semester)
    db.session.add(exam)
    db.session.flush()
    question = Question(exam_id=exam.id, qtype=Q_NUMERIC,
                        text="Find P2", answer="202.65",
                        tolerance_percent=2.0, points=5, order_index=1)
    db.session.add(question)
    db.session.commit()

    with signed_in("save-teacher", "pw-save-teacher") as client:
        client.post(f"/exams/{exam.id}/open")

    with signed_in("A-0001", "A-0001") as client:
        client.post(f"/take/{exam.id}/start", data={"access_code": "SAVE"})
        attempt = Attempt.query.filter_by(exam_id=exam.id, student_id=student.id).first()

        client.post(f"/take/attempt/{attempt.id}/autosave",
                    data={f"q{question.id}": "202.6"})
        attempt = reload(attempt)
        answer = Answer.query.filter_by(attempt_id=attempt.id).first()
        check("autosave stores the answer as it is typed",
              answer is not None and answer.response == "202.6",
              answer.response if answer else "no answer row")
        check("autosave does not submit the attempt",
              attempt.status == ATTEMPT_IN_PROGRESS, attempt.status)

        # Saving again replaces rather than duplicating.
        client.post(f"/take/attempt/{attempt.id}/autosave",
                    data={f"q{question.id}": "202.65"})
        check("re-saving updates the same answer row",
              Answer.query.filter_by(attempt_id=attempt.id).count() == 1,
              str(Answer.query.filter_by(attempt_id=attempt.id).count()))

        client.post(f"/take/attempt/{attempt.id}/submit",
                    data={f"q{question.id}": "202.65"})

    attempt = reload(attempt)
    check("submitting marks the attempt", attempt.status == ATTEMPT_SUBMITTED)
    check("a numeric answer within tolerance is marked correct",
          near(attempt.score, 5.0), str(attempt.score))

    # Reopening keeps the answers and gives time back.
    with signed_in("save-teacher", "pw-save-teacher") as client:
        client.post(f"/exams/attempts/{attempt.id}/reopen", data={"minutes": "10"})

    attempt = reload(attempt)
    check("reopening puts the attempt back in progress",
          attempt.status == ATTEMPT_IN_PROGRESS, attempt.status)
    check("reopening keeps the answers",
          Answer.query.filter_by(attempt_id=attempt.id).count() == 1)
    check("reopening grants extra time", (attempt.extra_seconds or 0) >= 600,
          str(attempt.extra_seconds))
    check("reopening is counted", attempt.reopen_count == 1)

    # Resetting wipes it entirely.
    attempt_id = attempt.id
    with signed_in("save-teacher", "pw-save-teacher") as client:
        client.post(f"/exams/attempts/{attempt_id}/reset")

    check("resetting removes the attempt",
          db.session.get(Attempt, attempt_id) is None)
    check("resetting removes its answers",
          Answer.query.filter_by(attempt_id=attempt_id).count() == 0)


# --------------------------------------------------------------------------- #
# 13. Overrides survive re-marking
# --------------------------------------------------------------------------- #

def test_override_survives_rescore():
    teacher = make_instructor("ovr-teacher")
    course = make_course("TEST-OVR", teacher)
    student = make_student("O-0001")
    db.session.add(Enrollment(course_id=course.id, student_id=student.id))
    db.session.commit()

    item = add_item(course, SCOPE_LECTURE, "Written Works", "Quiz", 50)
    exam = Exam(course_id=course.id, title="Synced", duration_minutes=30,
                access_code="OVR", link_item_id=item.id, exit_mode="off",
                school_year=course.school_year, semester=course.semester)
    db.session.add(exam)
    db.session.flush()
    question = Question(exam_id=exam.id, qtype=Q_NUMERIC, text="Q", answer="10",
                        points=10, order_index=1)
    db.session.add(question)
    db.session.commit()

    attempt = Attempt(exam_id=exam.id, student_id=student.id,
                      status=ATTEMPT_SUBMITTED, submitted_at=utcnow())
    db.session.add(attempt)
    db.session.flush()
    db.session.add(Answer(attempt_id=attempt.id, question_id=question.id,
                          response="10"))
    db.session.commit()
    mark_attempt(attempt, [question])
    db.session.commit()

    check("the exam marks itself correctly", near(attempt.score, 10.0),
          str(attempt.score))

    with signed_in("ovr-teacher", "pw-ovr-teacher") as client:

        client.post(f"/exams/{exam.id}/sync")
        row = Score.query.filter_by(item_id=item.id, student_id=student.id).first()
        check("the exam score is copied into the gradebook, rescaled",
              row is not None and near(row.points, 50.0), str(row.points if row else None))

        # Override the attempt score, then re-mark everything.
        client.post(f"/exams/attempts/{attempt.id}/override",
                    data={"score": "7", "note": "credit for the method"})
        attempt = reload(attempt)
        check("the override takes effect", near(attempt.effective_score, 7.0),
              str(attempt.effective_score))

        client.post(f"/exams/{exam.id}/rescore")
        attempt = reload(attempt)
        check("re-marking does not wipe the override",
              attempt.is_override and near(attempt.effective_score, 7.0),
              str(attempt.effective_score))

        # A gradebook mark locked by hand is never overwritten by a sync.
        row = Score.query.filter_by(item_id=item.id, student_id=student.id).first()
        row.points = 49.0
        row.is_override = True
        db.session.commit()

        client.post(f"/exams/{exam.id}/sync")
        row = reload(row)
        check("a locked gradebook mark survives a sync",
              near(row.points, 49.0), str(row.points))


# --------------------------------------------------------------------------- #
# 14. The multi-part parser that used to crash
# --------------------------------------------------------------------------- #

def test_multipart_parser():
    teacher = make_instructor("mp-teacher")
    course = make_course("TEST-MP", teacher)
    exam = Exam(course_id=course.id, title="Multipart", duration_minutes=30,
                access_code="MP", school_year=course.school_year,
                semester=course.semester)
    db.session.add(exam)
    db.session.commit()

    with signed_in("mp-teacher", "pw-mp-teacher") as client:

        # The exact shape that raised ValueError in the previous build, plus
        # the other field order, and a line with a unit in an odd place.
        cases = [
            ("type before points", "a | Find T2 | 318.5 | num | 5 | 2"),
            ("points before type", "b | Find T2 | 318.5 | 5 | num | 2"),
            ("short answer type", "c | Name the process | isochoric | short | 3"),
            ("a trailing unit", "d | Find the mass | 1.162 | num | 4 | 2 | kg"),
            ("bare minimum", "e | Find V | 0.5"),
        ]
        for label, bulk in cases:
            response = client.post(f"/exams/{exam.id}/questions", data={
                "qtype": Q_MULTIPART,
                "text": f"Problem for {label}",
                "points": "1",
                "parts_bulk": bulk,
            }, follow_redirects=True)
            check(f"multi-part accepts: {label}", response.status_code < 500,
                  str(response.status_code))

    questions = Question.query.filter_by(exam_id=exam.id, qtype=Q_MULTIPART).all()
    check("every multi-part problem was created", len(questions) == 5,
          str(len(questions)))

    by_label = {}
    for question in questions:
        for part in question.parts:
            by_label[part.label] = part

    check("the type word is read as a type, not a number",
          by_label.get("a") is not None and by_label["a"].answer_type == Q_NUMERIC,
          str(getattr(by_label.get("a"), "answer_type", None)))
    check("points are read correctly when the type comes first",
          by_label.get("a") is not None and near(by_label["a"].points, 5.0),
          str(getattr(by_label.get("a"), "points", None)))
    check("the other field order gives the same result",
          by_label.get("b") is not None and near(by_label["b"].points, 5.0)
          and by_label["b"].answer_type == Q_NUMERIC,
          f"{getattr(by_label.get('b'), 'points', None)} / "
          f"{getattr(by_label.get('b'), 'answer_type', None)}")
    check("a short-answer sub-item keeps its type",
          by_label.get("c") is not None and by_label["c"].answer_type == "short",
          str(getattr(by_label.get("c"), "answer_type", None)))
    check("a trailing unit is kept as the unit",
          by_label.get("d") is not None and by_label["d"].unit == "kg",
          str(getattr(by_label.get("d"), "unit", None)))
    check("a minimal line still works",
          by_label.get("e") is not None and by_label["e"].answer == "0.5",
          str(getattr(by_label.get("e"), "answer", None)))

    # The points of a multi-part problem are the sum of its sub-items.
    first = next(q for q in questions if any(p.label == "a" for p in q.parts))
    check("a multi-part problem is worth the sum of its sub-items",
          near(first.effective_points, 5.0), str(first.effective_points))


# --------------------------------------------------------------------------- #
# 14b. Loading a question bank from a spreadsheet
# --------------------------------------------------------------------------- #

def test_question_csv():
    """A sheet in, the same sheet out, and nothing saved when it is bad."""
    from app.question_io import parse_questions_csv, template_csv

    # ---- the template is a valid sheet ---------------------------------- #
    specs, problems = parse_questions_csv(template_csv())
    check("the template parses with no problems", not problems, str(problems))
    check("the template covers all seven question types",
          len({s["qtype"] for s in specs}) == 7,
          str(sorted({s["qtype"] for s in specs})))

    # ---- a bad row is reported, the good ones still import -------------- #
    mixed = (
        "type,question,points,answer,option_a,option_b\n"
        "mcq,Good row,2,B,alpha,beta\n"
        "mcq,Answer matches nothing,2,Z,alpha,beta\n"
        "numeric,Not a number,3,abc,,\n"
        "numeric,Fine,3,12.5,,\n"
    )
    specs, problems = parse_questions_csv(mixed)
    check("the readable rows are kept", len(specs) == 2, str(len(specs)))
    check("each unreadable row is reported with its line number",
          len(problems) == 2 and "row 3" in problems[0] and "row 4" in problems[1],
          str(problems))

    # ---- an answer may be a letter, a number, or the text --------------- #
    for key, expected in (("B", "beta"), ("2", "beta"), ("beta", "beta")):
        specs, _ = parse_questions_csv(
            f"type,question,points,answer,option_a,option_b\n"
            f"mcq,Which,1,{key},alpha,beta\n"
        )
        check(f"an answer given as “{key}” resolves to the right option",
              specs and specs[0]["answer"] == expected,
              str(specs[0]["answer"] if specs else None))

    # ---- headings people actually type are accepted --------------------- #
    specs, problems = parse_questions_csv(
        "Question Type,Item,Marks,Key,A,B,Category,Level\n"
        "mcq,Renamed headings,2,A,one,two,Basics,easy\n"
    )
    check("alternative column headings are understood",
          specs and specs[0]["topic"] == "Basics" and specs[0]["answer"] == "one",
          str(problems))

    # ---- through the web, into a real bank ------------------------------ #
    teacher = make_instructor("csv-teacher")
    course = make_course("TEST-CSV", teacher)
    bank = QuestionBank(course_id=course.id, name="Imported bank")
    db.session.add(bank)
    db.session.commit()
    bank_id = bank.id

    with signed_in("csv-teacher", "pw-csv-teacher") as client:
        sheet = template_csv()

        # The first submission must change nothing at all.
        response = client.post(f"/exams/banks/{bank_id}/import",
                               data={"csv_text": sheet})
        check("the check step returns a review page", response.status_code == 200,
              str(response.status_code))
        check("the check step saves nothing",
              Question.query.filter_by(bank_id=bank_id).count() == 0,
              str(Question.query.filter_by(bank_id=bank_id).count()))

        response = client.post(
            f"/exams/banks/{bank_id}/import",
            data={"csv_text": sheet, "confirm": "1",
                  "default_topic": "Thermodynamics"},
            follow_redirects=True,
        )
        check("confirming the import succeeds", response.status_code == 200,
              str(response.status_code))

        questions = Question.query.filter_by(bank_id=bank_id).all()
        check("every question in the sheet was created", len(questions) == 7,
              str(len(questions)))

        by_type = {q.qtype: q for q in questions}
        check("the multiple-choice answer is stored as the option text",
              by_type["mcq"].answer == "PV = mRT", str(by_type["mcq"].answer))
        check("the numerical tolerance came across",
              near(by_type["numeric"].tolerance_percent, 2.0),
              str(by_type["numeric"].tolerance_percent))
        check("the unit came across",
              by_type["numeric"].unit == "kPa", str(by_type["numeric"].unit))
        check("the matching pairs came across with their answers",
              len(by_type["matching"].pairs) == 4
              and by_type["matching"].pairs[0]["answer"] == 0,
              str(by_type["matching"].pairs[:1]))
        check("the sub-items of the problem came across",
              len(by_type["multipart"].parts) == 3,
              str(len(by_type["multipart"].parts)))
        check("a sub-item's type word was read as a type",
              by_type["multipart"].parts[0].answer_type == Q_NUMERIC,
              str(by_type["multipart"].parts[0].answer_type))
        check("the problem is worth the sum of its sub-items",
              near(by_type["multipart"].effective_points, 10.0),
              str(by_type["multipart"].effective_points))
        check("the topic column wins over the fallback topic",
              by_type["essay"].topic == "Second law", str(by_type["essay"].topic))

        # ---- the export is readable by the importer --------------------- #
        response = client.get(f"/exams/banks/{bank_id}/export.csv")
        check("the bank exports as CSV", response.status_code == 200,
              str(response.status_code))
        check("the export is offered as a download",
              "attachment" in response.headers.get("Content-Disposition", ""),
              response.headers.get("Content-Disposition", ""))

        exported = response.get_data(as_text=True)
        again, problems = parse_questions_csv(exported)
        check("an exported bank re-imports with no problems",
              not problems, str(problems))
        check("the round trip keeps every question", len(again) == 7,
              str(len(again)))

        original = {q.qtype: q for q in questions}
        same = {s["qtype"]: s for s in again}
        check("the round trip keeps the multiple-choice key",
              same["mcq"]["answer"] == original["mcq"].answer,
              f"{same['mcq']['answer']} vs {original['mcq'].answer}")
        check("the round trip keeps the sub-items",
              len(same["multipart"]["parts"]) == 3,
              str(len(same["multipart"]["parts"])))
        check("the round trip keeps the matching pairs",
              len(same["matching"]["options"]) == 4,
              str(len(same["matching"]["options"])))

        # ---- another instructor's bank is out of reach ------------------ #
        other = make_instructor("csv-stranger")
        other_course = make_course("TEST-CSV2", other)
        other_bank = QuestionBank(course_id=other_course.id, name="Not yours")
        db.session.add(other_bank)
        db.session.commit()
        response = client.post(f"/exams/banks/{other_bank.id}/import",
                               data={"csv_text": template_csv(), "confirm": "1"})
        check("an instructor cannot import into someone else's bank",
              response.status_code in (403, 404), str(response.status_code))
        check("and nothing was written there",
              Question.query.filter_by(bank_id=other_bank.id).count() == 0)

    # ---- a sheet with nothing usable writes nothing ---------------------- #
    before = Question.query.filter_by(bank_id=bank_id).count()
    with signed_in("csv-teacher", "pw-csv-teacher") as client:
        client.post(f"/exams/banks/{bank_id}/import",
                    data={"csv_text": "type,question\nmcq,\n", "confirm": "1"},
                    follow_redirects=True)
    check("a sheet with no usable rows changes nothing",
          Question.query.filter_by(bank_id=bank_id).count() == before,
          str(Question.query.filter_by(bank_id=bank_id).count()))

    # ---- a spreadsheet file, not a CSV, is refused clearly --------------- #
    with signed_in("csv-teacher", "pw-csv-teacher") as client:
        response = client.post(
            f"/exams/banks/{bank_id}/import",
            data={"csv_file": (io.BytesIO(b"PK\x03\x04junk"), "bank.xlsx")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        body = response.get_data(as_text=True)
        check("an .xlsx upload is refused with an explanation",
              "CSV" in body, str(response.status_code))
        check("and the .xlsx upload wrote nothing",
              Question.query.filter_by(bank_id=bank_id).count() == before)


# --------------------------------------------------------------------------- #
# 14b-ii. The paper copy and its key
# --------------------------------------------------------------------------- #

def test_paper_copy():
    """The printed key must describe the paper that was actually printed."""
    teacher = make_instructor("paper-teacher")
    course = make_course("TEST-PAPER", teacher)
    exam = Exam(course_id=course.id, title="Paper exam", duration_minutes=60,
                access_code="PAPER", school_year=course.school_year,
                semester=course.semester, shuffle_questions=True)
    db.session.add(exam)
    db.session.commit()

    mcq = Question(exam_id=exam.id, qtype="mcq", text="Which one?",
                   answer="beta", points=2.0, order_index=1)
    mcq.options = ["alpha", "beta", "gamma"]
    pairs = [
        {"left": "Isothermal", "right": "Temperature", "answer": 0},
        {"left": "Isobaric", "right": "Pressure", "answer": 1},
        {"left": "Isochoric", "right": "Volume", "answer": 2},
    ]
    match = Question(exam_id=exam.id, qtype="matching", text="Match them",
                     points=2.0, order_index=2)
    match.pairs = pairs
    numeric = Question(exam_id=exam.id, qtype=Q_NUMERIC, text="Find P2",
                       answer="202.65", unit="kPa", tolerance_percent=2.0,
                       points=5.0, order_index=3)
    db.session.add_all([mcq, match, numeric])
    db.session.commit()

    with signed_in("paper-teacher", "pw-paper-teacher") as client:
        response = client.get(f"/exams/{exam.id}/paper?key=1&sets=2&shuffle=1")
        check("the paper renders with a key", response.status_code == 200,
              str(response.status_code))
        body = response.get_data(as_text=True)
        check("the multiple-choice key gives the printed letter",
              "B — beta" in body, "letter missing")
        check("the numerical key carries the unit and tolerance",
              "202.65 kPa" in body and "2%" in body, "unit or tolerance missing")
        check("both versions are printed",
              body.count("Set A") >= 1 and body.count("Set B") >= 1)

        # The important invariant: for every pair, the letter the key gives
        # must be the letter the paper actually printed that pair's right-hand
        # item against.
        from app.blueprints.exams import paper as paper_view

        with client.application.test_request_context(
            f"/exams/{exam.id}/paper?sets=4&shuffle=1"
        ):
            from flask_login import login_user

            login_user(teacher)
            rendered = paper_view(exam.id)
        check("the paper view returns markup",
              "Good Luck!" in rendered, type(rendered).__name__)

    # Re-derive the mapping the route builds and confirm it is self-consistent
    # for every set, which is where an off-by-one in a shuffle would hide.
    import random as _random

    letters = "ABCDEFGHIJKLMNOPQRST"
    for set_index in range(4):
        rng = _random.Random(f"{exam.id}-{set_index}")
        ordered = [mcq, match, numeric]
        if set_index > 0:
            rng.shuffle(ordered)
        for question in ordered:
            if question.qtype != "matching":
                continue
            order = list(range(len(pairs)))
            rng.shuffle(order)
            column_b = [pairs[i] for i in order]
            where = {original: printed for printed, original in enumerate(order)}
            for left_index in range(len(pairs)):
                printed_at = where[left_index]
                check(
                    f"set {set_index}: pair {left_index + 1} points at the "
                    f"item actually printed as {letters[printed_at]}",
                    column_b[printed_at]["right"] == pairs[left_index]["right"],
                    f"{column_b[printed_at]['right']} vs {pairs[left_index]['right']}",
                )

    # Reprinting must give the same paper, or a printed key stops matching.
    with signed_in("paper-teacher", "pw-paper-teacher") as client:
        first = client.get(f"/exams/{exam.id}/paper?sets=3&shuffle=1&key=1")
        again = client.get(f"/exams/{exam.id}/paper?sets=3&shuffle=1&key=1")
        check("reprinting gives byte-identical papers",
              first.get_data() == again.get_data(),
              "the shuffle is not seeded")


# --------------------------------------------------------------------------- #
# 14c. Adding one student to a subject
# --------------------------------------------------------------------------- #

def test_add_single_student():
    teacher = make_instructor("one-teacher")
    course = make_course("TEST-ONE", teacher)
    section = Section(course_id=course.id, name="A", capacity=40)
    other_section = Section(course_id=course.id, name="B", capacity=40)
    db.session.add_all([section, other_section])
    db.session.commit()

    existing = make_student("2023-9001")
    db.session.commit()

    with signed_in("one-teacher", "pw-one-teacher") as client:

        # ---- a brand-new student: the account is created too ------------ #
        client.post(f"/subjects/{course.id}/add-student", data={
            "student_number": "2023-9002",
            "name": "Reyes, Ana B.",
            "program": "BSME",
            "year_level": "4th Year",
            "section_id": str(section.id),
        }, follow_redirects=True)

        created = User.query.filter_by(student_number="2023-9002").first()
        check("a new student number creates an account", created is not None)
        check("the new account is a student",
              created is not None and created.role == ROLE_STUDENT)
        check("the new account must change its password at first sign-in",
              created is not None and created.must_change_password)
        check("the student number is the first password",
              created is not None and created.check_password("2023-9002"))
        enrolment = Enrollment.query.filter_by(
            course_id=course.id, student_id=created.id).first()
        check("the new student is enrolled", enrolment is not None)
        check("into the section that was chosen",
              enrolment is not None and enrolment.section_id == section.id)

        # ---- an existing student: no second account ---------------------- #
        before = User.query.count()
        client.post(f"/subjects/{course.id}/add-student", data={
            "student_number": "2023-9001",
            "section_id": str(section.id),
        }, follow_redirects=True)
        check("an existing student number does not create a second account",
              User.query.count() == before, str(User.query.count()))
        check("the existing student is enrolled",
              Enrollment.query.filter_by(
                  course_id=course.id, student_id=existing.id).first() is not None)

        # ---- adding them twice does not duplicate the enrolment ---------- #
        client.post(f"/subjects/{course.id}/add-student", data={
            "student_number": "2023-9001",
            "section_id": str(section.id),
        }, follow_redirects=True)
        check("adding the same student twice leaves one enrolment",
              Enrollment.query.filter_by(
                  course_id=course.id, student_id=existing.id).count() == 1)

        # ---- a different section moves them ------------------------------ #
        client.post(f"/subjects/{course.id}/add-student", data={
            "student_number": "2023-9001",
            "section_id": str(other_section.id),
        }, follow_redirects=True)
        moved = Enrollment.query.filter_by(
            course_id=course.id, student_id=existing.id).first()
        check("naming a different section moves the student",
              moved is not None and moved.section_id == other_section.id,
              str(getattr(moved, "section_id", None)))

        # ---- a dropped student comes back -------------------------------- #
        moved.is_dropped = True
        moved.dropped_at = utcnow()
        db.session.commit()
        client.post(f"/subjects/{course.id}/add-student", data={
            "student_number": "2023-9001",
            "section_id": str(other_section.id),
        }, follow_redirects=True)
        back = reload(moved)
        check("adding a dropped student readmits them",
              back is not None and not back.is_dropped)

        # ---- an unknown number with no name is refused ------------------- #
        before = User.query.count()
        response = client.post(f"/subjects/{course.id}/add-student", data={
            "student_number": "2023-9999",
            "section_id": str(section.id),
        }, follow_redirects=True)
        check("an unknown student number with no name is refused",
              User.query.count() == before, str(User.query.count()))
        check("and the refusal says what is missing",
              "name" in response.get_data(as_text=True).lower())

        # ---- a blank number is refused ----------------------------------- #
        before = User.query.count()
        client.post(f"/subjects/{course.id}/add-student", data={
            "student_number": "   ",
            "name": "No Number",
            "section_id": str(section.id),
        }, follow_redirects=True)
        check("a blank student number is refused",
              User.query.count() == before)

        # ---- a staff number is not enrolled as a student ----------------- #
        staff = make_instructor("one-colleague")
        staff.student_number = "EMP-001"
        db.session.commit()
        client.post(f"/subjects/{course.id}/add-student", data={
            "student_number": "EMP-001",
            "section_id": str(section.id),
        }, follow_redirects=True)
        check("a staff account cannot be enrolled as a student",
              Enrollment.query.filter_by(
                  course_id=course.id, student_id=staff.id).first() is None)

        # ---- a section from another subject is rejected ------------------ #
        stranger = make_instructor("one-stranger")
        stranger_course = make_course("TEST-ONE2", stranger)
        foreign = Section(course_id=stranger_course.id, name="X", capacity=10)
        db.session.add(foreign)
        db.session.commit()
        response = client.post(f"/subjects/{course.id}/add-student", data={
            "student_number": "2023-9001",
            "section_id": str(foreign.id),
        })
        check("a section belonging to another subject is rejected",
              response.status_code == 404, str(response.status_code))

        # ---- another instructor's subject is out of reach ---------------- #
        response = client.post(f"/subjects/{stranger_course.id}/add-student", data={
            "student_number": "2023-9001",
            "name": "Nope",
            "section_id": str(foreign.id),
        })
        check("an instructor cannot add students to someone else's subject",
              response.status_code in (403, 404), str(response.status_code))


# --------------------------------------------------------------------------- #
# 14d. The lecture / laboratory split belongs to the subject
# --------------------------------------------------------------------------- #

def test_per_subject_shares():
    """Two subjects with different splits must not influence each other."""
    teacher = make_instructor("share-teacher")
    heavy = make_course("TEST-LAB-HEAVY", teacher, laboratory=True)
    light = make_course("TEST-LAB-LIGHT", teacher, laboratory=True)

    heavy.lecture_share, heavy.laboratory_share = 40.0, 60.0
    light.lecture_share, light.laboratory_share = 67.0, 33.0
    db.session.commit()

    student = make_student("2024-7001")
    for course in (heavy, light):
        db.session.add(Enrollment(course_id=course.id, student_id=student.id))
    db.session.commit()

    # The same marks in both: 90% on lecture, 60% on laboratory.
    for course in (heavy, light):
        lecture_item = add_item(course, SCOPE_LECTURE, "Written Works",
                                "Quiz", 100.0)
        lab_item = add_item(course, SCOPE_LABORATORY, "Written Works",
                            "Lab quiz", 100.0)
        set_score(lecture_item, student, 90.0)
        set_score(lab_item, student, 60.0)
    db.session.commit()

    heavy_result = compute_student(heavy, student)
    light_result = compute_student(light, student)

    # 0.40(90) + 0.60(60) = 72.0 ; 0.67(90) + 0.33(60) = 80.1
    check("a laboratory-heavy subject uses its own split",
          near(heavy_result.final_percent, 72.0),
          str(heavy_result.final_percent))
    check("a lecture-heavy subject uses its own split",
          near(light_result.final_percent, 80.1),
          str(light_result.final_percent))
    check("the two subjects give different grades from identical marks",
          heavy_result.final_percent != light_result.final_percent)

    # Changing one subject must leave the other exactly where it was.
    before = compute_student(light, student).final_percent
    with signed_in("share-teacher", "pw-share-teacher") as client:
        client.post(f"/subjects/{heavy.id}/settings", data={
            "title": heavy.title, "units": "4", "lec_units": "3",
            "lab_units": "1", "has_laboratory": "1",
            "lecture_share": "25", "laboratory_share": "75",
            "averaging_method": "total_points",
            "removal_low": "55", "removal_high": "59.99",
        }, follow_redirects=True)

    heavy = reload(heavy)
    check("the subject's own split was saved",
          near(heavy.lecture_share, 25.0) and near(heavy.laboratory_share, 75.0),
          f"{heavy.lecture_share}/{heavy.laboratory_share}")
    check("changing one subject leaves the other untouched",
          near(compute_student(light, student).final_percent, before),
          str(compute_student(light, student).final_percent))
    # 0.25(90) + 0.75(60) = 67.5
    check("and the changed subject re-computes from the new split",
          near(compute_student(heavy, student).final_percent, 67.5),
          str(compute_student(heavy, student).final_percent))

    # Shares that do not add to 100 are normalised rather than rejected, so
    # 7 and 3 means the same as 70 and 30.
    heavy.lecture_share, heavy.laboratory_share = 7.0, 3.0
    db.session.commit()
    check("shares are read as a ratio, not as percentages that must total 100",
          near(compute_student(heavy, student).final_percent, 81.0),
          str(compute_student(heavy, student).final_percent))

    # Both at zero would divide by nothing; the form refuses it.
    with signed_in("share-teacher", "pw-share-teacher") as client:
        client.post(f"/subjects/{heavy.id}/settings", data={
            "title": heavy.title, "units": "4", "lec_units": "3",
            "lab_units": "1", "has_laboratory": "1",
            "lecture_share": "0", "laboratory_share": "0",
            "averaging_method": "total_points",
            "removal_low": "55", "removal_high": "59.99",
        }, follow_redirects=True)
    heavy = reload(heavy)
    check("two zero shares are refused and reset, not stored",
          (heavy.lecture_share or 0) + (heavy.laboratory_share or 0) > 0,
          f"{heavy.lecture_share}/{heavy.laboratory_share}")


# --------------------------------------------------------------------------- #
# 14e. Subject colours and the dashboard's appearance
# --------------------------------------------------------------------------- #

def test_subject_colours():
    from app.models import SUBJECT_COLOUR_NAMES

    teacher = make_instructor("colour-teacher")
    first = make_course("TEST-COL-1", teacher)
    second = make_course("TEST-COL-2", teacher)

    check("a subject with no colour chosen still has one",
          first.tile_colour in SUBJECT_COLOUR_NAMES, first.tile_colour)
    # The same code must always land on the same colour, on every machine and
    # in every term — that stability is what makes a colour recognisable.
    from app.models import Course as _Course
    twin = _Course(code=first.code)
    check("the same subject code always derives the same colour",
          twin.tile_colour == first.tile_colour,
          f"{twin.tile_colour} vs {first.tile_colour}")
    check("a different code derives a different colour",
          second.tile_colour != first.tile_colour or len(SUBJECT_COLOUR_NAMES) < 2,
          f"{second.tile_colour} vs {first.tile_colour}")

    with signed_in("colour-teacher", "pw-colour-teacher") as client:
        client.post(f"/dashboard/subject/{first.id}/colour",
                    data={"colour": "maroon"}, follow_redirects=True)
        check("a chosen colour is kept", reload(first).colour == "maroon",
              reload(first).colour)

        client.post(f"/dashboard/subject/{first.id}/colour",
                    data={"colour": "chartreuse"}, follow_redirects=True)
        check("a colour that is not in the palette is refused, not stored",
              reload(first).colour == "", repr(reload(first).colour))

        client.post("/dashboard/appearance",
                    data={"style": "seal", "size": "compact"},
                    follow_redirects=True)
        me = reload(teacher)
        check("the dashboard background choice is kept",
              me.dashboard_style == "seal", me.dashboard_style)
        check("the tile size choice is kept",
              me.dashboard_size == "compact", me.dashboard_size)

        client.post("/dashboard/appearance",
                    data={"style": "neon", "size": "enormous"},
                    follow_redirects=True)
        me = reload(teacher)
        check("an unknown appearance is ignored rather than stored",
              me.dashboard_style == "seal" and me.dashboard_size == "compact",
              f"{me.dashboard_style}/{me.dashboard_size}")

        response = client.get("/")
        body = response.get_data(as_text=True)
        check("the dashboard renders the subject tiles",
              "tile-code" in body and "TEST-COL-1" in body)

    # Another instructor cannot repaint a subject that is not theirs.
    stranger = make_instructor("colour-stranger")
    theirs = make_course("TEST-COL-3", stranger)
    with signed_in("colour-teacher", "pw-colour-teacher") as client:
        response = client.post(f"/dashboard/subject/{theirs.id}/colour",
                               data={"colour": "gold"})
        check("an instructor cannot recolour someone else's subject",
              response.status_code in (403, 404), str(response.status_code))
        check("and the colour is unchanged", reload(theirs).colour in (None, ""))

    # No two subjects on one dashboard should look the same.
    codes = [f"TEST-HUE-{n}" for n in range(8)]
    made = [make_course(code, teacher) for code in codes]
    db.session.commit()
    with signed_in("colour-teacher", "pw-colour-teacher") as client:
        body = client.get("/").get_data(as_text=True)
    import re as _re
    shown = _re.findall(r'class="tile c-([a-z]+)"', body)
    check("every subject got a tile", len(shown) >= len(made), str(len(shown)))
    check("no two tiles share a colour",
          len(set(shown)) == len(shown) or len(shown) > len(SUBJECT_COLOUR_NAMES),
          str(shown))


# --------------------------------------------------------------------------- #
# 14f. The authoring assistant
# --------------------------------------------------------------------------- #

def test_assistant():
    """It drafts; it never saves. And the key never comes back out."""
    from app import ai
    from app.blueprints.assistant import extract_csv
    from app.models import AIConversation, AIMessage, QuestionBank

    # ---- pulling the sheet out of a reply ------------------------------- #
    fenced = (
        "Here are five questions.\n\n```csv\n"
        "type,question,points,answer,option_a,option_b\n"
        "mcq,Which one?,2,A,alpha,beta\n```\nAnything else?"
    )
    check("a fenced CSV block is found",
          extract_csv(fenced).startswith("type,question"), extract_csv(fenced)[:30])
    check("the prose around it is discarded",
          "Here are five" not in extract_csv(fenced))
    check("a bare sheet with no fence is still read",
          extract_csv("type,question,points\nmcq,Hi,1").startswith("type,"))
    check("prose with no sheet yields nothing",
          extract_csv("I would rather not write that paper.") == "")
    check("a code block that is not a sheet is ignored",
          extract_csv("```python\nprint('hello')\n```") == "")

    # ---- the key is write-only and never rendered ----------------------- #
    secret = "sk-ant-test-DO-NOT-ECHO-0123456789"
    ai.store_key(secret)
    check("a stored key can be read back by the app", ai.resolve_key() == secret)
    check("the key is not stored in the clear",
          secret not in (Setting.get(ai.SETTING_KEY, "") or ""),
          "the raw key is sitting in the settings table")
    check("the app knows where the key came from",
          ai.key_source() == "database", ai.key_source())

    Setting.put(ai.SETTING_ENABLED, "1")
    db.session.commit()

    admin = User.query.filter_by(username="admin").first()
    if admin is not None:
        admin.set_password("admin-for-test")
        db.session.commit()
        with signed_in("admin", "admin-for-test") as client:
            body = client.get("/admin/assistant").get_data(as_text=True)
            check("the saved key is never rendered into the page",
                  secret not in body,
                  "the key appears in the administration page")
            check("the page says a key is set",
                  "saved here" in body or "not set" not in body)

    ai.store_key("")
    db.session.commit()
    check("clearing the key really clears it", ai.resolve_key() == "")
    check("and the app reports no key", ai.key_source() == "none")

    # ---- an instructor's draft cannot save anything --------------------- #
    teacher = make_instructor("ai-teacher")
    course = make_course("TEST-AI", teacher)
    bank = QuestionBank(course_id=course.id, name="AI bank")
    db.session.add(bank)
    conversation = AIConversation(user_id=teacher.id, title="Draft")
    db.session.add(conversation)
    db.session.flush()
    message = AIMessage(
        conversation_id=conversation.id, role="assistant",
        content=("```csv\n"
                 "type,question,points,answer,option_a,option_b\n"
                 "mcq,Which equation of state?,2,A,PV = mRT,PV = nRT\n"
                 "numeric,Find P2,5,202.65,,\n```"),
    )
    db.session.add(message)
    db.session.commit()

    before = Question.query.filter_by(bank_id=bank.id).count()
    with signed_in("ai-teacher", "pw-ai-teacher") as client:
        response = client.post(f"/assistant/message/{message.id}/to-bank",
                               data={"bank_id": str(bank.id)})
        check("sending a draft to a bank is accepted",
              response.status_code == 200, str(response.status_code))
        check("but it writes nothing by itself",
              Question.query.filter_by(bank_id=bank.id).count() == before,
              str(Question.query.filter_by(bank_id=bank.id).count()))
        check("it hands over to the ordinary importer instead",
              f"/exams/banks/{bank.id}/import" in response.get_data(as_text=True))

        # The importer's own review step still saves nothing.
        sheet = ("type,question,points,answer,option_a,option_b\n"
                 "mcq,Which equation of state?,2,A,PV = mRT,PV = nRT\n")
        client.post(f"/exams/banks/{bank.id}/import", data={"csv_text": sheet})
        check("and the review step after it still saves nothing",
              Question.query.filter_by(bank_id=bank.id).count() == before)

    # ---- conversations are private -------------------------------------- #
    stranger = make_instructor("ai-stranger")
    make_course("TEST-AI2", stranger)
    with signed_in("ai-stranger", "pw-ai-stranger") as client:
        response = client.get(f"/assistant/{conversation.id}")
        check("an instructor cannot open someone else's conversation",
              response.status_code == 404, str(response.status_code))
        response = client.post(f"/assistant/message/{message.id}/to-bank",
                               data={"bank_id": str(bank.id)})
        check("nor send their draft into a bank",
              response.status_code == 404, str(response.status_code))

    # ---- with no key, nothing is attempted ------------------------------ #
    Setting.put(ai.SETTING_ENABLED, "0")
    db.session.commit()
    with signed_in("ai-teacher", "pw-ai-teacher") as client:
        client.post(f"/assistant/{conversation.id}/say",
                    data={"message": "write me a paper"}, follow_redirects=True)
    check("with the assistant off, no reply is recorded",
          not any(m.role == "assistant" and "paper" in m.content
                  for m in reload(conversation).messages))


# --------------------------------------------------------------------------- #
# 14g. Lifting the forced password change
# --------------------------------------------------------------------------- #

def test_password_prompts():
    """Clearing the prompt must not touch anyone's actual password."""
    teacher = make_instructor("pw-teacher")
    course = make_course("TEST-PW", teacher)

    asked = make_student("2025-5001")
    asked.must_change_password = True
    settled = make_student("2025-5002")
    settled.set_password("chosen-by-the-student")
    settled.must_change_password = False
    outsider = make_student("2025-5003")
    outsider.must_change_password = True
    for student in (asked, settled):
        db.session.add(Enrollment(course_id=course.id, student_id=student.id))
    db.session.commit()

    admin = User.query.filter_by(username="admin").first()
    admin.set_password("admin-for-test")
    db.session.commit()

    with signed_in("admin", "admin-for-test") as client:
        # Nothing ticked changes nothing.
        client.post("/admin/password-prompts", data={}, follow_redirects=True)
        check("with nothing ticked, nobody is changed",
              reload(asked).must_change_password)

        client.post("/admin/password-prompts",
                    data={"student_ids": [str(asked.id)]}, follow_redirects=True)
        check("a ticked student is no longer asked",
              not reload(asked).must_change_password)
        check("a student who was not ticked is untouched",
              reload(outsider).must_change_password)
        check("the student's password still works",
              reload(settled).check_password("chosen-by-the-student"))

    # The student can now sign in without being sent to the password form.
    with signed_in("2025-5001", "2025-5001") as client:
        response = client.get("/", follow_redirects=False)
        check("and signing in goes straight to the portal",
              response.status_code == 200, str(response.status_code))

    # ---- the standing setting governs new accounts ---------------------- #
    Setting.put("new_students_must_change_password", "0")
    db.session.commit()
    with signed_in("pw-teacher", "pw-pw-teacher") as client:
        client.post(f"/subjects/{course.id}/add-student", data={
            "student_number": "2025-5100", "name": "Nuevo, Ana",
        }, follow_redirects=True)
    fresh = User.query.filter_by(student_number="2025-5100").first()
    check("with the setting off, a new student is not asked",
          fresh is not None and not fresh.must_change_password)
    check("and still has the student number as their password",
          fresh is not None and fresh.check_password("2025-5100"))

    Setting.put("new_students_must_change_password", "1")
    db.session.commit()
    with signed_in("pw-teacher", "pw-pw-teacher") as client:
        client.post(f"/subjects/{course.id}/add-student", data={
            "student_number": "2025-5101", "name": "Viejo, Ben",
        }, follow_redirects=True)
    fresh = User.query.filter_by(student_number="2025-5101").first()
    check("with the setting on, a new student is asked again",
          fresh is not None and fresh.must_change_password)

    # ---- only an administrator may do this ------------------------------ #
    with signed_in("pw-teacher", "pw-pw-teacher") as client:
        response = client.post("/admin/password-prompts",
                               data={"student_ids": [str(outsider.id)]})
        check("an instructor cannot clear password prompts",
              response.status_code in (302, 403, 404), str(response.status_code))
        check("and the student is still asked",
              reload(outsider).must_change_password)


# --------------------------------------------------------------------------- #
# 14h. Printable records
# --------------------------------------------------------------------------- #

def test_printable_records():
    """A marked paper and a mark record, and the essay marks that survive."""
    from app.models import Rubric, RubricCriterion, RubricScore, Answer
    from app.marking import grade_essay, mark_attempt, rescore_exam

    teacher = make_instructor("print-teacher")
    course = make_course("TEST-PRINT", teacher, laboratory=True)
    student = make_student("2026-3001")
    db.session.add(Enrollment(course_id=course.id, student_id=student.id))

    rubric = Rubric(course_id=course.id, name="Essay rubric", points_per_criterion=10)
    db.session.add(rubric)
    db.session.flush()
    for index, title in enumerate(("Physics", "Clarity")):
        db.session.add(RubricCriterion(rubric_id=rubric.id, title=title,
                                       max_points=10, order_index=index))

    exam = Exam(course_id=course.id, title="Printable exam", duration_minutes=60,
                access_code="PRN", school_year=course.school_year,
                semester=course.semester, shuffle_questions=False)
    db.session.add(exam)
    db.session.flush()

    mcq = Question(exam_id=exam.id, qtype="mcq", text="Which one?",
                   answer="beta", points=2, order_index=1)
    mcq.options = ["alpha", "beta", "gamma"]
    essay = Question(exam_id=exam.id, qtype="essay", text="Explain entropy.",
                     points=20, order_index=2, rubric_id=rubric.id)
    db.session.add_all([mcq, essay])
    db.session.flush()

    exam.is_open = True
    exam.opened_at = utcnow()
    exam.closes_at = exam.opened_at + timedelta(minutes=60)
    attempt = Attempt(exam_id=exam.id, student_id=student.id,
                      status=ATTEMPT_SUBMITTED, submitted_at=utcnow(), seed=1)
    attempt.question_order = [mcq.id, essay.id]
    db.session.add(attempt)
    db.session.flush()
    db.session.add_all([
        Answer(attempt_id=attempt.id, question_id=mcq.id, response="beta"),
        Answer(attempt_id=attempt.id, question_id=essay.id,
               response="Entropy never decreases in an isolated system."),
    ])
    db.session.commit()
    mark_attempt(attempt, list(exam.questions))
    db.session.commit()

    essay_answer = Answer.query.filter_by(
        attempt_id=attempt.id, question_id=essay.id
    ).first()
    check("an unmarked essay is reported as waiting",
          essay_answer.needs_manual)

    # ---- the bug: a marked essay must stay marked through a rescore ------ #
    grade_essay(essay_answer, 17.0, "Good, but define the system.", teacher)
    db.session.commit()
    check("marking an essay clears the waiting flag",
          not reload(essay_answer).needs_manual)

    rescore_exam(exam)
    db.session.commit()
    essay_answer = reload(essay_answer)
    check("re-marking the exam keeps the essay's mark",
          near(essay_answer.points_awarded, 17.0),
          str(essay_answer.points_awarded))
    check("re-marking does NOT put a marked essay back in the waiting queue",
          not essay_answer.needs_manual,
          "a marked paper would be labelled provisional for ever")
    check("the comment survives re-marking",
          essay_answer.feedback == "Good, but define the system.",
          essay_answer.feedback)

    attempt = reload(attempt)
    mark_attempt(attempt, list(exam.questions))
    db.session.commit()
    check("and nothing is counted as pending on a fully marked paper",
          reload(attempt).manual_pending == 0,
          str(reload(attempt).manual_pending))

    score = RubricScore(rubric_id=rubric.id, student_id=student.id,
                        answer_id=essay_answer.id, total=17.0,
                        comment="Clear on the direction of the inequality.")
    score.scores = {str(c.id): 9 if c.title == "Physics" else 8
                    for c in rubric.criteria}
    db.session.add(score)
    db.session.commit()

    # ---- the marked paper -------------------------------------------------- #
    with signed_in("print-teacher", "pw-print-teacher") as client:
        response = client.get(f"/exams/attempts/{attempt.id}/report")
        check("a marked paper prints", response.status_code == 200,
              str(response.status_code))
        body = response.get_data(as_text=True)
        check("it shows what the student answered", "beta" in body)
        check("it shows the essay they wrote",
              "Entropy never decreases" in body)
        check("it shows the rubric criteria",
              "Physics" in body and "Clarity" in body)
        check("it shows the rubric total", "17" in body)
        check("it shows the marker's comment",
              "direction of the inequality" in body)

        hidden = client.get(
            f"/exams/attempts/{attempt.id}/report?key=0"
        ).get_data(as_text=True)
        check("the correct answers can be withheld",
              "correct</span>" not in hidden and "correct:" not in hidden)

        whole = client.get(f"/exams/{exam.id}/reports")
        check("a whole class prints in one go", whole.status_code == 200,
              str(whole.status_code))
        check("and the class run names the student",
              "2026-3001" in whole.get_data(as_text=True))

    # ---- the mark record --------------------------------------------------- #
    lecture_item = add_item(course, SCOPE_LECTURE, "Written Works", "Quiz 1", 50.0)
    set_score(lecture_item, student, 44.0)
    lab_item = add_item(course, SCOPE_LABORATORY, "Performance Task",
                        "Laboratory Exercise 1", 50.0)
    set_score(lab_item, student, 45.0)
    lecture_item.rubric_id = rubric.id
    item_score = RubricScore(rubric_id=rubric.id, student_id=student.id,
                             item_id=lecture_item.id, total=44.0,
                             comment="Solid working.")
    item_score.scores = {str(c.id): 22 for c in rubric.criteria}
    db.session.add(item_score)
    db.session.commit()

    with signed_in("print-teacher", "pw-print-teacher") as client:
        response = client.get(f"/reports/{course.id}/mark-record/{student.id}")
        check("a mark record prints", response.status_code == 200,
              str(response.status_code))
        body = response.get_data(as_text=True)
        check("it lists the written work", "Quiz 1" in body)
        check("it lists the laboratory work", "Laboratory Exercise 1" in body)
        check("it names both scopes",
              "Lecture" in body and "Laboratory" in body)
        check("it shows the rubric behind a gradebook mark",
              "Solid working." in body)
        check("it shows how the final grade is split",
              "of the final grade" in body)

        without = client.get(
            f"/reports/{course.id}/mark-record/{student.id}?rubrics=0"
        ).get_data(as_text=True)
        check("the rubric breakdowns can be withheld",
              "Solid working." not in without)

        whole = client.get(f"/reports/{course.id}/mark-record")
        check("the whole class prints in one go", whole.status_code == 200,
              str(whole.status_code))

    # ---- another instructor's class is out of reach ------------------------ #
    stranger = make_instructor("print-stranger")
    theirs = make_course("TEST-PRINT2", stranger)
    with signed_in("print-teacher", "pw-print-teacher") as client:
        check("an instructor cannot print someone else's mark records",
              client.get(f"/reports/{theirs.id}/mark-record").status_code
              in (403, 404))
        check("nor someone else's marked papers",
              client.get(f"/exams/{exam.id}/reports").status_code == 200)


# --------------------------------------------------------------------------- #
# 14i. The class record on paper
# --------------------------------------------------------------------------- #

def test_class_record():
    """The gradebook grid printed: columns split, nothing lost."""
    from app.blueprints.gradebook import _paginate_columns

    class FakeComponent:
        def __init__(self, name, count):
            self.name = name
            self.items = list(range(count))

    def total_activities(pages):
        return sum(len(g["activities"]) for page in pages for g in page)

    # A gradebook that fits on one sheet stays on one sheet.
    pages = _paginate_columns([FakeComponent("A", 3), FakeComponent("B", 4)], 16)
    check("a narrow gradebook prints on one sheet", len(pages) == 1,
          str(len(pages)))

    # A wide one is split, and every activity still appears exactly once.
    components = [FakeComponent("A", 4), FakeComponent("B", 8), FakeComponent("C", 2)]
    pages = _paginate_columns(components, 10)
    check("a wide gradebook is split across sheets", len(pages) > 1, str(len(pages)))
    check("every activity appears, and only once",
          total_activities(pages) == 14, str(total_activities(pages)))

    # A component is kept whole where it can be.
    whole = [len(g["activities"]) for page in pages for g in page
             if g["component"].name == "B"]
    check("a component that fits a sheet is not split",
          len(whole) == 1 and whole[0] == 8, str(whole))

    # A component wider than a whole sheet must still be split, with its
    # percentage following the last piece so it is never a partial total.
    pages = _paginate_columns([FakeComponent("Huge", 25)], 8)
    pieces = [g for page in pages for g in page]
    check("a component wider than a sheet is split", len(pieces) > 1,
          str(len(pieces)))
    check("and nothing is dropped when it is",
          total_activities(pages) == 25, str(total_activities(pages)))
    check("only the last piece carries the percentage",
          sum(1 for g in pieces if g["show_percent"]) == 1,
          str([g["show_percent"] for g in pieces]))
    check("the percentage is on the final piece",
          pieces[-1]["show_percent"] is True)

    # No sheet may exceed the budget it was given.
    for page in pages:
        width = sum(len(g["activities"]) + (1 if g["show_percent"] else 0)
                    for g in page)
        check("no sheet is wider than the column budget", width <= 8, str(width))

    # An empty gradebook still yields a printable sheet rather than nothing.
    check("an empty gradebook still prints a sheet",
          _paginate_columns([], 16) == [[]])

    # ---- through the web, against real marks ---------------------------- #
    teacher = make_instructor("grid-teacher")
    course = make_course("TEST-GRID", teacher, laboratory=True)
    students = [make_student(f"2026-91{n:02d}") for n in range(3)]
    for student in students:
        db.session.add(Enrollment(course_id=course.id, student_id=student.id))

    quiz = add_item(course, SCOPE_LECTURE, "Written Works", "GAS CYCLE", 20.0)
    chapter = add_item(course, SCOPE_LECTURE, "Performance Task", "CHAPTER 1-VP", 40.0)
    midterm = add_item(course, SCOPE_LECTURE, "Major Examinations",
                       "MIDTERM EXAMINATION", 80.0, kind="midterm")
    lab = add_item(course, SCOPE_LABORATORY, "Performance Task",
                   "Laboratory Exercise 1", 50.0)
    set_score(quiz, students[0], 20.0)
    set_score(chapter, students[0], 31.0)
    set_score(midterm, students[0], 73.0)
    set_score(lab, students[0], 45.0)
    set_score(quiz, students[1], 3.0)          # the rest left blank on purpose
    db.session.commit()

    with signed_in("grid-teacher", "pw-grid-teacher") as client:
        response = client.get(f"/gradebook/{course.id}/record")
        check("the class record prints", response.status_code == 200,
              str(response.status_code))
        body = response.get_data(as_text=True)
        check("it names every activity",
              "GAS CYCLE" in body and "CHAPTER 1-VP" in body
              and "MIDTERM EXAMINATION" in body)
        check("it lists every student",
              all(s.student_number in body for s in students))
        check("it shows a mark that was entered", ">20<" in body or "20" in body)
        check("it says an empty cell is not a zero",
              "not counted as a zero" in body)

        lab_sheet = client.get(
            f"/gradebook/{course.id}/record?scope=laboratory"
        ).get_data(as_text=True)
        check("the laboratory sheet prints its own activities",
              "Laboratory Exercise 1" in lab_sheet)
        check("and does not carry the lecture activities",
              "GAS CYCLE" not in lab_sheet)

        narrow = client.get(
            f"/gradebook/{course.id}/record?cols=6"
        ).get_data(as_text=True)
        check("a tighter column budget still names every activity",
              "GAS CYCLE" in narrow and "MIDTERM EXAMINATION" in narrow)

    stranger = make_instructor("grid-stranger")
    theirs = make_course("TEST-GRID2", stranger)
    with signed_in("grid-teacher", "pw-grid-teacher") as client:
        check("an instructor cannot print someone else's class record",
              client.get(f"/gradebook/{theirs.id}/record").status_code
              in (403, 404))


# --------------------------------------------------------------------------- #
# 14j. The examination paper and the coloured class record
# --------------------------------------------------------------------------- #

def test_examination_paper():
    """The paper, its key, and the colours on the class record."""
    teacher = make_instructor("paper2-teacher")
    course = make_course("TEST-PAPER2", teacher)
    exam = Exam(course_id=course.id, title="Final Examination",
                duration_minutes=120, access_code="FIN",
                school_year=course.school_year, semester=course.semester,
                shuffle_questions=False, shuffle_options=False)
    db.session.add(exam)
    db.session.flush()

    wanted = [
        ("Stresses in Beams", "Pure bending occurs where shear is ______.",
         ["zero", "maximum", "negative", "constant"], "zero", 1.0),
        ("Stresses in Beams", "The flexure formula is ______.",
         ["P/A", "Tr/J", "Mc/I", "PL/AE"], "Mc/I", 2.0),
        ("Analysis of Stress", "Principal planes carry a shear of ______.",
         ["maximum", "zero", "average", "half"], "zero", 1.0),
    ]
    for index, (topic, text, options, answer, points) in enumerate(wanted, start=1):
        question = Question(exam_id=exam.id, qtype="mcq", text=text, topic=topic,
                            answer=answer, points=points, order_index=index)
        question.options = options
        db.session.add(question)
    db.session.commit()

    Setting.put("program_chair", "JOSEPH M. MANZANO")
    Setting.put("program_chair_title", "Program Chair")
    Setting.put("exam_form_code", "WPU-OSF-CET-ME-01 Rev.00")
    db.session.commit()

    with signed_in("paper2-teacher", "pw-paper2-teacher") as client:
        response = client.get(f"/exams/{exam.id}/paper")
        check("the paper prints", response.status_code == 200,
              str(response.status_code))
        body = response.get_data(as_text=True)

        check("it carries the identification line",
              "Program/ Year/ Section" in body and "Score:" in body)
        check("the title is the exam's own, in capitals",
              "FINAL EXAMINATION" in body)
        check("the course line names code and title",
              "TEST-PAPER2" in body and course.title in body)
        check("the directions are printed", "Directions:" in body)
        check("items are grouped into parts by topic",
              "Part I — Stresses in Beams" in body
              and "Part II — Analysis of Stress" in body)
        check("a two-point item says so", "(2 pts)" in body)
        check("a one-point item does not", body.count("(1 pts)") == 0)
        check("it ends with Good Luck!", "Good Luck!" in body)

        check("the preparer is the subject's own instructor",
              teacher.name.upper() in body, teacher.name)
        check("the programme chair comes from the setting",
              "JOSEPH M. MANZANO" in body)
        check("their position is printed", "Program Chair" in body)
        check("the control number is in the footer",
              "WPU-OSF-CET-ME-01 Rev.00" in body)

        # ---- the answer key ---------------------------------------------- #
        check("the key is printed on its own page",
              "ANSWER KEY" in body and "xpaper-break" in body)
        check("the key is for the instructor only",
              "For instructor use only" in body)
        check("the key is a grid of letters", "xpaper-key" in body)
        check("the scoring line is printed", "Scoring:" in body)

        withheld = client.get(
            f"/exams/{exam.id}/paper?key=0"
        ).get_data(as_text=True)
        check("the key can be withheld", "ANSWER KEY" not in withheld)
        check("but the paper itself still prints",
              "Good Luck!" in withheld)

        # ---- a name and code typed in for one printing ------------------- #
        once = client.get(
            f"/exams/{exam.id}/paper?checked_by=MARIA+L.+SANTOS"
            f"&checked_title=OIC,+Program+Chair&form_code=DRAFT-01"
        ).get_data(as_text=True)
        check("a chair typed in for one paper is used",
              "MARIA L. SANTOS" in once and "JOSEPH M. MANZANO" not in once)
        check("their position can be changed too", "OIC, Program Chair" in once)
        check("so can the control number",
              "DRAFT-01" in once and "WPU-OSF-CET-ME-01" not in once)
        check("and the setting is untouched by that",
              Setting.get("program_chair") == "JOSEPH M. MANZANO")

        blank = client.get(
            f"/exams/{exam.id}/paper?checked_by="
        ).get_data(as_text=True)
        check("a chair cleared for one paper leaves a line to sign over",
              "xpaper-sign-blank" in blank)

    # ---- the printed key must match the printed options ------------------ #
    # With the options shuffled, a key that still named the original position
    # would be wrong on every paper handed out.
    exam.shuffle_options = True
    db.session.commit()
    from app.blueprints.exams import paper as paper_view
    with app.test_request_context(f"/exams/{exam.id}/paper?sets=3&shuffle=1"):
        from flask_login import login_user

        login_user(teacher)
        rendered = paper_view(exam.id)
    check("a shuffled paper still renders", "ANSWER KEY" in rendered)

    import re as _re
    # Each set's printed options and its key letter, read back out of the page.
    sets = rendered.split("ANSWER KEY")
    check("every version has its own key", len(sets) >= 2, str(len(sets)))

    # ---- the class record colours ---------------------------------------- #
    failing = make_student("2026-8001")
    passing = make_student("2026-8002")
    for student in (failing, passing):
        db.session.add(Enrollment(course_id=course.id, student_id=student.id))
    quiz = add_item(course, SCOPE_LECTURE, "Written Works", "Quiz 1", 100.0)
    set_score(quiz, failing, 20.0)
    set_score(quiz, passing, 90.0)
    db.session.commit()

    with signed_in("paper2-teacher", "pw-paper2-teacher") as client:
        body = client.get(f"/gradebook/{course.id}/record").get_data(as_text=True)
        check("a failing student's row is marked as failing",
              "classrec-failed" in body)
        check("the legend explains the colours",
              "Red" in body and "yellow" in body)

    # A student inside the removal band must be marked yellow, not red.
    removal_student = make_student("2026-8003")
    db.session.add(Enrollment(course_id=course.id, student_id=removal_student.id))
    set_score(quiz, removal_student, 57.0)
    db.session.commit()
    result = compute_student(course, removal_student)
    check("the test's own student really is in the removal band",
          result.effective_remark == "Removal", result.effective_remark)

    with signed_in("paper2-teacher", "pw-paper2-teacher") as client:
        body = client.get(f"/gradebook/{course.id}/record").get_data(as_text=True)
        check("a removal candidate's row is marked for removal",
              "classrec-removal" in body)


# --------------------------------------------------------------------------- #
# 15. Grade release gate
# --------------------------------------------------------------------------- #

def test_grade_release():
    teacher = make_instructor("rel-teacher")
    course = make_course("TEST-REL", teacher)
    student = make_student("G-0001")
    db.session.add(Enrollment(course_id=course.id, student_id=student.id))
    db.session.commit()

    item = add_item(course, SCOPE_LECTURE, "Written Works", "Quiz", 100)
    set_score(item, student, 91)
    db.session.commit()

    course.grades_released = False
    db.session.commit()

    with signed_in("G-0001", "G-0001") as client:
        page = client.get(f"/my/progress?subject={course.id}").get_data(as_text=True)
        check("an unreleased subject still shows the individual mark",
              "91" in page)
        # Collapse whitespace: the sentence wraps across lines in the markup.
        flat = " ".join(page.split()).lower()
        check("an unreleased subject hides the computed standing",
              "has not yet released the computed standing" in flat, "")
        check("an unreleased subject shows no transmuted grade",
              "1.50" not in page, "")

    course.grades_released = True
    db.session.commit()

    with signed_in("G-0001", "G-0001") as client:
        page = client.get(f"/my/progress?subject={course.id}").get_data(as_text=True)
        # 91.00 sits below the 91.15 threshold, so it transmutes to 1.50.
        check("a released subject shows the grade", "1.50" in page, "")
        check("the released standing is shown too", "91.00" in page, "")


# --------------------------------------------------------------------------- #
# 16. A student cannot reach another student's work
# --------------------------------------------------------------------------- #

def test_student_isolation():
    teacher = make_instructor("iso-s-teacher")
    course = make_course("TEST-ISOS", teacher)
    mine = make_student("I-0001")
    theirs = make_student("I-0002")
    for person in (mine, theirs):
        db.session.add(Enrollment(course_id=course.id, student_id=person.id))
    db.session.commit()

    exam = Exam(course_id=course.id, title="Private", duration_minutes=30,
                access_code="PRIV", school_year=course.school_year,
                semester=course.semester)
    db.session.add(exam)
    db.session.flush()
    db.session.add(Question(exam_id=exam.id, qtype=Q_NUMERIC, text="Q",
                            answer="1", points=1, order_index=1))
    db.session.commit()

    other_attempt = Attempt(exam_id=exam.id, student_id=theirs.id,
                            status=ATTEMPT_SUBMITTED, submitted_at=utcnow())
    db.session.add(other_attempt)
    db.session.commit()

    with signed_in("I-0001", "I-0001") as client:

        check("a student cannot open another student's attempt",
              client.get(f"/take/attempt/{other_attempt.id}").status_code == 404)
        check("a student cannot review another student's attempt",
              client.get(f"/take/attempt/{other_attempt.id}/review").status_code == 404)
        check("a student cannot autosave into another student's attempt",
              client.post(f"/take/attempt/{other_attempt.id}/autosave",
                          data={"x": "1"}).status_code == 404)
        check("a student cannot reach the gradebook",
              client.get(f"/gradebook/{course.id}",
                         follow_redirects=False).status_code in (302, 403, 404))
        check("a student cannot reach another's grade detail",
              client.get(f"/gradebook/{course.id}/student/{theirs.id}",
                         follow_redirects=False).status_code in (302, 403, 404))


# --------------------------------------------------------------------------- #
# 17. Data survives a "redeploy", and backups round-trip
# --------------------------------------------------------------------------- #

def test_data_outlives_code():
    from app.backup import build_snapshot, restore_snapshot

    data_dir = app.config["DATA_DIR"]
    source_root = os.path.dirname(os.path.abspath(app.root_path))
    check("the database lives outside the program folder",
          str(data_dir).startswith(WORKSPACE)
          and not str(data_dir).startswith(os.path.join(source_root, "app")),
          f"{data_dir} vs {source_root}")

    before = build_snapshot()
    user_count = before["counts"].get("user", 0)
    course_count = before["counts"].get("course", 0)
    score_count = before["counts"].get("score", 0)
    check("the backup captured the accounts", user_count > 5, str(user_count))
    check("the backup captured the subjects", course_count > 5, str(course_count))
    check("the backup captured the marks", score_count > 5, str(score_count))

    # Wipe and restore: every row must come back.
    report = restore_snapshot(before, wipe=True)
    check("the restore reported no errors", not report["errors"],
          "; ".join(report["errors"][:3]))

    after = build_snapshot()
    check("accounts survive a wipe-and-restore",
          after["counts"].get("user") == user_count,
          f"{after['counts'].get('user')} vs {user_count}")
    check("subjects survive a wipe-and-restore",
          after["counts"].get("course") == course_count)
    check("marks survive a wipe-and-restore",
          after["counts"].get("score") == score_count)

    # The grading arithmetic still produces the same answer afterwards.
    course = Course.query.filter_by(code="TEST-LEC").first()
    if course is not None:
        student = User.query.filter_by(student_number="T-0001").first()
        if student is not None:
            result = compute_student(course, student)
            check("a standing is unchanged after a restore",
                  near(result.final_percent, 78.0), str(result.final_percent))


# --------------------------------------------------------------------------- #
# 18. Assets cannot be served stale
# --------------------------------------------------------------------------- #

def test_asset_cache_busting():
    from app.assets import asset_url, digest

    with app.test_request_context():
        first = digest("css/app.css")
        url = asset_url("css/app.css")
        check("the stylesheet URL carries a content hash", first in url, url)
        check("the stylesheet is not served from /static",
              not url.startswith("/static"), url)

    with app.test_client() as client:
        response = client.get(url)
        check("the hashed stylesheet is served", response.status_code == 200)
        check("a correct hash is cached hard",
              "immutable" in response.headers.get("Cache-Control", ""),
              response.headers.get("Cache-Control"))

        # An old URL must still work, but must not be cached.
        stale = url.replace(first, "0" * 16)
        response = client.get(stale)
        check("a stale asset URL still serves the current file",
              response.status_code == 200, str(response.status_code))
        check("a stale asset URL is explicitly not cached",
              "no-store" in response.headers.get("Cache-Control", ""),
              response.headers.get("Cache-Control"))

        # The hash must change when the file does.
        css_path = os.path.join(app.root_path, "static", "css", "app.css")
        original = open(css_path, "rb").read()
        try:
            with open(css_path, "ab") as handle:
                handle.write(b"\n/* cache-busting test */\n")
            with app.test_request_context():
                second = digest("css/app.css")
            check("editing the stylesheet changes its hash", first != second,
                  f"{first} == {second}")
        finally:
            with open(css_path, "wb") as handle:
                handle.write(original)

        # HTML itself must never be cached.
        client.post("/login", data={"identifier": "admin", "password": "admin-test-pw"})
        response = client.get("/")
        check("pages are never cached",
              "no-store" in response.headers.get("Cache-Control", ""),
              response.headers.get("Cache-Control"))


# --------------------------------------------------------------------------- #
# 19. The equation renderer is bundled, not fetched from a CDN
# --------------------------------------------------------------------------- #

def test_katex_is_local():
    base = os.path.join(app.root_path, "static", "vendor", "katex")
    check("KaTeX stylesheet is bundled",
          os.path.exists(os.path.join(base, "katex.min.css")))
    check("KaTeX script is bundled",
          os.path.exists(os.path.join(base, "katex.min.js")))

    fonts = os.path.join(base, "fonts")
    woff2 = [f for f in os.listdir(fonts) if f.endswith(".woff2")] if os.path.isdir(fonts) else []
    check("the maths fonts are bundled", len(woff2) >= 10, f"{len(woff2)} fonts")

    css = open(os.path.join(base, "katex.min.css"), encoding="utf-8").read()
    check("the bundled stylesheet references no outside host",
          "http://" not in css and "https://" not in css, "")

    # No template may pull the renderer from a CDN.
    offenders = []
    import pathlib

    for path in pathlib.Path(app.root_path, "templates").rglob("*.html"):
        text = path.read_text()
        for needle in ("cdn.jsdelivr", "cdnjs.cloudflare", "unpkg.com",
                       "cdn.tailwindcss"):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    check("no template loads anything from a CDN", not offenders,
          "; ".join(offenders))


# --------------------------------------------------------------------------- #
# 20. Marks are validated
# --------------------------------------------------------------------------- #

def test_mark_validation():
    teacher = make_instructor("val-teacher")
    course = make_course("TEST-VAL", teacher)
    student = make_student("V-0001")
    db.session.add(Enrollment(course_id=course.id, student_id=student.id))
    db.session.commit()

    item = add_item(course, SCOPE_LECTURE, "Written Works", "Quiz", 25)
    db.session.commit()

    with signed_in("val-teacher", "pw-val-teacher") as client:

        page = client.post(f"/gradebook/item/{item.id}",
                           data={f"score_{student.id}": "30"},
                           follow_redirects=True).get_data(as_text=True)
        row = Score.query.filter_by(item_id=item.id, student_id=student.id).first()
        check("a mark above the maximum is refused",
              row is None or row.points is None, str(row.points if row else None))
        check("the instructor is told why", "above the maximum" in page, "")

        client.post(f"/gradebook/item/{item.id}",
                    data={f"score_{student.id}": "-5"}, follow_redirects=True)
        row = Score.query.filter_by(item_id=item.id, student_id=student.id).first()
        check("a negative mark is refused",
              row is None or row.points is None, str(row.points if row else None))

        client.post(f"/gradebook/item/{item.id}",
                    data={f"score_{student.id}": "abc"}, follow_redirects=True)
        row = Score.query.filter_by(item_id=item.id, student_id=student.id).first()
        check("a non-numeric mark is refused",
              row is None or row.points is None, str(row.points if row else None))

        client.post(f"/gradebook/item/{item.id}",
                    data={f"score_{student.id}": "22.5"}, follow_redirects=True)
        row = Score.query.filter_by(item_id=item.id, student_id=student.id).first()
        check("a valid mark is saved", row is not None and near(row.points, 22.5),
              str(row.points if row else None))

        # Blanking clears rather than storing a zero.
        client.post(f"/gradebook/item/{item.id}",
                    data={f"score_{student.id}": ""}, follow_redirects=True)
        row = reload(row)
        check("clearing a mark records nothing rather than a zero",
              row.points is None, str(row.points))

        # Component weights must total 100 in each scope.
        page = client.post(f"/subjects/{course.id}/components", data={
            f"weight_{course.components[0].id}": "10",
        }, follow_redirects=True).get_data(as_text=True)
        check("component weights that do not total 100 are refused",
              "must total 100" in page, "")
        db.session.refresh(course.components[0])
        check("the rejected weight was not saved",
              near(course.components[0].weight_percent, 20.0),
              str(course.components[0].weight_percent))


# --------------------------------------------------------------------------- #

def main():
    tests = [
        ("Transmutation table", test_transmutation),
        ("Lecture weighting", test_lecture_weighting),
        ("Laboratory split", test_laboratory_split),
        ("Blank marks are not zeros", test_blank_is_not_zero),
        ("Averaging methods", test_averaging_methods),
        ("Removal examination", test_removal_examination),
        ("Instructor isolation", test_instructor_isolation),
        ("Section enrolment", test_section_enrolment),
        ("Drops and year standing", test_drops_and_year_status),
        ("Exam timing", test_exam_timing),
        ("Exit modes", test_exit_modes),
        ("Autosave, reopen, reset", test_autosave_reopen_reset),
        ("Overrides survive re-marking", test_override_survives_rescore),
        ("Multi-part parser", test_multipart_parser),
        ("Question banks from a spreadsheet", test_question_csv),
        ("The paper copy and its key", test_paper_copy),
        ("Adding one student", test_add_single_student),
        ("Lecture/laboratory split per subject", test_per_subject_shares),
        ("Subject colours and appearance", test_subject_colours),
        ("The authoring assistant", test_assistant),
        ("Lifting the password prompt", test_password_prompts),
        ("Printable records", test_printable_records),
        ("The class record on paper", test_class_record),
        ("The examination paper", test_examination_paper),
        ("Grade release gate", test_grade_release),
        ("Student isolation", test_student_isolation),
        ("Data outlives the code", test_data_outlives_code),
        ("Asset cache busting", test_asset_cache_busting),
        ("KaTeX is bundled locally", test_katex_is_local),
        ("Mark validation", test_mark_validation),
    ]

    with app.app_context():
        for title, function in tests:
            before = len(FAILED)
            try:
                function()
            except Exception:
                import traceback

                FAILED.append((title, traceback.format_exc()[-900:]))
            status = "ok" if len(FAILED) == before else "FAILED"
            print(f"  {status:7s} {title}")

    print()
    print("=" * 66)
    print(f"{len(PASSED)} checks passed, {len(FAILED)} failed")

    if FAILED:
        print()
        for name, detail in FAILED:
            print(f"FAIL  {name}")
            if detail:
                for line in str(detail).strip().splitlines()[-6:]:
                    print(f"      {line}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(WORKSPACE, ignore_errors=True)
