"""Behaviour tests for the rules that matter.

Status codes are covered by smoke.py.  This file checks that the app actually
does the right thing: the CET arithmetic, the removal examination, instructor
isolation, exam timing, the exit modes, and that a redeploy cannot lose data.

Runs against a throw-away database, so it never touches real records.

    python test_rules.py
"""
from __future__ import annotations

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

    # Strict: leaving submits at once.
    exam, student = fresh_exam("strict", "X-0001")
    with signed_in("X-0001", "X-0001") as client:
        client.post(f"/take/{exam.id}/start", data={"access_code": "EXIT"})
        attempt = Attempt.query.filter_by(exam_id=exam.id, student_id=student.id).first()

        client.post(f"/take/attempt/{attempt.id}/event",
                    data={"kind": "hidden", "detail": "switched apps"})
        attempt = reload(attempt)
        check("strict mode submits when the student leaves",
              attempt.status == ATTEMPT_SUBMITTED, attempt.status)
        check("the reason records that they left",
              (attempt.submit_reason or "").startswith("exit"),
              attempt.submit_reason)

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
                        data={"kind": "hidden", "detail": "switched apps"})
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
