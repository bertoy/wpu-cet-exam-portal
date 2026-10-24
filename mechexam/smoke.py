"""End-to-end smoke test.

Seeds a realistic subject with sections, students, a gradebook, an exam of
every question type and a sat attempt, then walks every GET route as each role
and reports anything that is not a success or an intentional redirect.

Run it with:  python smoke.py
"""
from __future__ import annotations

import sys
import traceback
from datetime import date, timedelta

import logging

from app import create_app
from app.extensions import db
from app.grading import ensure_components
from app.models import (
    Answer,
    Attempt,
    ATTEMPT_SUBMITTED,
    AttendanceRecord,
    AttendanceSession,
    Course,
    Enrollment,
    Exam,
    GradeComponent,
    GradeItem,
    Message,
    Q_ESSAY,
    Q_MATCHING,
    Q_MCQ,
    Q_MULTIPART,
    Q_NUMERIC,
    Q_SHORT,
    Q_TRUEFALSE,
    Question,
    QuestionBank,
    ROLE_INSTRUCTOR,
    ROLE_STUDENT,
    ROLE_SUPERADMIN,
    Rubric,
    SCOPE_LABORATORY,
    SCOPE_LECTURE,
    Score,
    Section,
    Setting,
    SubQuestion,
    Submission,
    User,
    utcnow,
)
from app.marking import mark_attempt

app = create_app()
app.config["TESTING"] = False
# Let real exceptions reach the test instead of being swallowed by the 500
# page, whose own HTML would be all the report ever showed.
app.config["PROPAGATE_EXCEPTIONS"] = True
# The report below is the output that matters; Flask's own traceback dump for
# every 500 would bury it.
logging.getLogger("app").setLevel(logging.ERROR)
app.logger.setLevel(logging.CRITICAL)


def _describe(body: str) -> str:
    """Pull the useful line out of an error page or traceback."""
    for marker in ("TemplateNotFound: ", "UndefinedError: ", "TypeError: ",
                   "AttributeError: ", "OperationalError: ", "ValueError: ",
                   "KeyError: ", "TemplateSyntaxError: ", "TemplateAssertionError: "):
        index = body.rfind(marker)
        if index != -1:
            return body[index:index + 220].splitlines()[0]
    tail = [line for line in body.strip().splitlines() if line.strip()]
    return tail[-1][:220] if tail else ""


# --------------------------------------------------------------------------- #

def seed():
    with app.app_context():
        if Course.query.filter_by(code="ME 412").first():
            print("  (already seeded)")
            return

        year, semester = Setting.term()

        instructor = User.query.filter_by(username="rabrasaldo").first()
        if instructor is None:
            instructor = User(
                username="rabrasaldo",
                role=ROLE_INSTRUCTOR,
                name="Robert V. Abrasaldo",
                rank="Instructor I",
            )
            instructor.set_password("teach123")
            db.session.add(instructor)
            db.session.flush()

        course = Course(
            code="ME 412",
            title="Thermodynamics 1",
            description="Properties of pure substances, first and second laws.",
            units=4.0, lec_units=3.0, lab_units=1.0,
            has_laboratory=True,
            owner_id=instructor.id,
            school_year=year, semester=semester,
        )
        db.session.add(course)
        db.session.flush()
        ensure_components(course)

        plain = Course(
            code="ME 321", title="Machine Design 1",
            units=3.0, lec_units=3.0, has_laboratory=False,
            owner_id=instructor.id, school_year=year, semester=semester,
        )
        db.session.add(plain)
        db.session.flush()
        ensure_components(plain)

        sections = []
        for name, schedule in (("BSME 4-A", "MWF 7:30–9:00"), ("BSME 4-B", "TTh 1:00–2:30")):
            section = Section(course_id=course.id, name=name, schedule=schedule,
                              room="ENG 201", capacity=40)
            db.session.add(section)
            sections.append(section)
        db.session.flush()

        students = []
        for n in range(1, 13):
            student = User(
                username=f"2023-{n:04d}",
                student_number=f"2023-{n:04d}",
                role=ROLE_STUDENT,
                name=f"Student {chr(64 + n)}. Surname{n:02d}",
                program="BSME", year_level="4th Year",
            )
            student.set_password(student.student_number)
            db.session.add(student)
            students.append(student)
        db.session.flush()

        for index, student in enumerate(students):
            db.session.add(
                Enrollment(course_id=course.id, student_id=student.id,
                           section_id=sections[index % 2].id)
            )
            db.session.add(Enrollment(course_id=plain.id, student_id=student.id))
        db.session.flush()

        # One dropped student, to exercise that path.
        dropped = Enrollment.query.filter_by(
            course_id=course.id, student_id=students[-1].id
        ).first()
        dropped.is_dropped = True
        dropped.dropped_at = utcnow()
        dropped.drop_note = "Dropped on the second week."

        # --- gradebook activities ------------------------------------------ #
        rubric = Rubric.query.filter_by(kind="report", is_template=True).first()
        local_rubric = Rubric(
            course_id=course.id, name="Technical Report (ME 412)",
            kind="report", points_per_criterion=10.0,
        )
        db.session.add(local_rubric)
        db.session.flush()
        if rubric is not None:
            from app.models import RubricCriterion

            for criterion in rubric.criteria:
                db.session.add(
                    RubricCriterion(
                        rubric_id=local_rubric.id, title=criterion.title,
                        description=criterion.description,
                        max_points=criterion.max_points,
                        order_index=criterion.order_index,
                    )
                )

        items = {}
        plans = [
            (SCOPE_LECTURE, "Written Works", "Quiz 1 — Pure Substances", "quiz", 25),
            (SCOPE_LECTURE, "Written Works", "Quiz 2 — First Law", "quiz", 30),
            (SCOPE_LECTURE, "Performance Task", "Technical Report", "report", 50),
            (SCOPE_LECTURE, "Major Examinations", "Midterm Examination", "midterm", 100),
            (SCOPE_LECTURE, "Major Examinations", "Final Examination", "final", 100),
            (SCOPE_LABORATORY, "Written Works", "Lab Quiz 1", "quiz", 20),
            (SCOPE_LABORATORY, "Performance Task", "Laboratory Report 1", "lab_report", 50),
            (SCOPE_LABORATORY, "Major Examinations", "Laboratory Practical", "final", 60),
        ]
        for scope, component_name, title, kind, maximum in plans:
            component = next(
                c for c in course.components
                if c.scope == scope and c.name == component_name
            )
            item = GradeItem(
                component_id=component.id, title=title, item_type=kind,
                max_points=maximum, date_given=date.today() - timedelta(days=10),
                order_index=len(items) + 1,
            )
            if kind in ("report", "lab_report"):
                item.rubric_id = local_rubric.id
                item.accepts_submission = True
                item.instructions = "Submit the report as a single PDF."
            db.session.add(item)
            db.session.flush()
            items[title] = item

        # Marks, spread so the class contains passes, removals and failures.
        import random

        random.seed(7)
        for position, student in enumerate(students):
            if student.id == students[-1].id:
                continue
            # Engineer a few students into each outcome band.
            if position < 6:
                band = random.uniform(0.80, 0.97)
            elif position < 9:
                band = random.uniform(0.62, 0.74)
            elif position < 11:
                band = random.uniform(0.555, 0.598)      # removal band
            else:
                band = random.uniform(0.30, 0.50)

            for title, item in items.items():
                jitter = random.uniform(-0.04, 0.04)
                points = max(0.0, min(item.max_points,
                                      round(item.max_points * (band + jitter), 1)))
                db.session.add(
                    Score(item_id=item.id, student_id=student.id, points=points)
                )

        # --- an exam with every question type ------------------------------- #
        exam = Exam(
            course_id=course.id,
            title="Quiz 3 — Ideal Gases",
            category="Quiz",
            topic="Ideal gas law, processes",
            instructions="Answer all items. Show the unit where one is asked for.",
            duration_minutes=45,
            access_code="ME412A",
            exit_mode="strict",
            shuffle_questions=True,
            shuffle_options=True,
            show_score=True,
            show_answers=True,
            school_year=year, semester=semester,
            created_by_id=instructor.id,
            link_item_id=items["Quiz 2 — First Law"].id,
        )
        db.session.add(exam)
        db.session.flush()

        questions = [
            Question(
                exam_id=exam.id, qtype=Q_MCQ, order_index=1, points=2,
                text=r"For an ideal gas, which relation is the equation of state? "
                     r"$PV = mRT$ uses the specific gas constant $R$.",
                answer="PV = mRT",
                options_raw='["PV = mRT", "PV = nRT^{2}", "P/V = mRT", "PVT = mR"]',
                topic="Ideal gas", difficulty="easy",
                explanation="The ideal gas equation of state is $PV = mRT$.",
            ),
            Question(
                exam_id=exam.id, qtype=Q_TRUEFALSE, order_index=2, points=1,
                text="An isothermal process on an ideal gas has no change in internal energy.",
                answer="True", options_raw='["True", "False"]',
                topic="Processes",
            ),
            Question(
                exam_id=exam.id, qtype=Q_NUMERIC, order_index=3, points=5,
                text=r"Air at $P_{1} = 101.325\ \text{kPa}$ and $T_{1} = 300\ \text{K}$ "
                     r"is compressed isothermally to half its volume. "
                     r"Find $P_{2}$ in kPa.",
                answer="202.65", tolerance_percent=2.0, unit="kPa",
                topic="Ideal gas", difficulty="moderate",
            ),
            Question(
                exam_id=exam.id, qtype=Q_SHORT, order_index=4, points=2,
                text="Name the law that relates pressure and volume at constant temperature.",
                answer="Boyle's law | Boyles law | Boyle",
                topic="Gas laws",
            ),
            Question(
                exam_id=exam.id, qtype=Q_MATCHING, order_index=5, points=2,
                text="Match each process with the property that stays constant.",
                options_raw=(
                    '[{"left": "Isothermal", "right": "Temperature", "answer": 0},'
                    ' {"left": "Isobaric", "right": "Pressure", "answer": 1},'
                    ' {"left": "Isochoric", "right": "Volume", "answer": 2},'
                    ' {"left": "Adiabatic", "right": "Heat transfer is zero", "answer": 3}]'
                ),
                topic="Processes",
            ),
            Question(
                exam_id=exam.id, qtype=Q_ESSAY, order_index=6, points=10,
                text="Explain, with reference to the second law, why a heat engine "
                     "cannot convert all the heat it receives into work.",
                rubric_id=local_rubric.id,
                topic="Second law", difficulty="hard",
            ),
            Question(
                exam_id=exam.id, qtype=Q_MULTIPART, order_index=7, points=1,
                text=r"A rigid tank of $0.5\ \text{m}^{3}$ holds air at "
                     r"$200\ \text{kPa}$ and $27^{\circ}\text{C}$. "
                     r"Take $R = 0.287\ \text{kJ/kg}\cdot\text{K}$.",
                topic="Ideal gas", difficulty="hard",
            ),
        ]
        for question in questions:
            db.session.add(question)
        db.session.flush()

        multipart = questions[-1]
        for label, text, answer, kind, points, tolerance, unit in [
            ("a", "Find the mass of air in the tank.", "1.162", "numeric", 4, 2.0, "kg"),
            ("b", "Find the specific volume.", "0.4304", "numeric", 3, 2.0, "m^3/kg"),
            ("c", "Name the process if the tank is heated.", "isochoric", "short", 3, 0, ""),
        ]:
            db.session.add(
                SubQuestion(
                    question_id=multipart.id, label=label, text=text,
                    answer=answer, answer_type=kind, points=points,
                    tolerance_percent=tolerance, unit=unit,
                    order_index=ord(label) - 96,
                )
            )
        db.session.flush()

        # A question bank, for the random-draw feature.
        bank = QuestionBank(
            course_id=course.id, name="Thermodynamics item pool",
            description="Reusable items, tagged by topic.",
        )
        db.session.add(bank)
        db.session.flush()
        for n in range(1, 7):
            db.session.add(
                Question(
                    bank_id=bank.id, qtype=Q_NUMERIC,
                    text=rf"Bank item {n}: find $T_{{2}}$ when $P_{{2}} = {n * 100}\ \text{{kPa}}$.",
                    answer=str(290 + n * 7), tolerance_percent=2.0, points=3,
                    topic="Ideal gas" if n % 2 else "Processes",
                    difficulty="moderate",
                )
            )

        # --- a sat attempt -------------------------------------------------- #
        exam.is_open = True
        exam.opened_at = utcnow()
        exam.closes_at = exam.opened_at + timedelta(minutes=exam.duration_minutes)
        db.session.flush()

        for student in students[:5]:
            attempt = Attempt(
                exam_id=exam.id, student_id=student.id,
                status=ATTEMPT_SUBMITTED, started_at=utcnow(),
                submitted_at=utcnow(), seed=1234 + student.id,
            )
            attempt.question_order = [q.id for q in questions]
            db.session.add(attempt)
            db.session.flush()

            responses = {
                questions[0].id: "PV = mRT",
                questions[1].id: "True",
                questions[2].id: "202.6",
                questions[3].id: "Boyle's Law",
                questions[4].id: "0=0\n1=1\n2=2\n3=3",
                questions[5].id: "A heat engine must reject heat to a sink…",
            }
            for question_id, response in responses.items():
                db.session.add(
                    Answer(attempt_id=attempt.id, question_id=question_id,
                           response=response)
                )
            for part in multipart.parts:
                db.session.add(
                    Answer(attempt_id=attempt.id, question_id=multipart.id,
                           sub_question_id=part.id, response=part.answer)
                )
            db.session.flush()
            mark_attempt(attempt, questions)

        # --- attendance, a submission and a message ------------------------- #
        session_row = AttendanceSession(
            course_id=course.id, section_id=sections[0].id,
            meeting_date=date.today() - timedelta(days=3),
            topic="First law applied to closed systems",
        )
        db.session.add(session_row)
        db.session.flush()
        for index, student in enumerate(students[:6]):
            db.session.add(
                AttendanceRecord(
                    session_id=session_row.id, student_id=student.id,
                    status="absent" if index == 3 else ("late" if index == 1 else "present"),
                )
            )

        db.session.add(
            Submission(
                item_id=items["Technical Report"].id,
                student_id=students[0].id,
                filename="report.pdf", stored_path="", mime_type="application/pdf",
                size_bytes=12345, note="Submitted on time.",
            )
        )
        db.session.add(
            Message(
                sender_id=instructor.id, course_id=course.id,
                subject="Quiz 3 on Friday",
                body="Bring a calculator. Coverage is the ideal gas law and processes.",
                is_announcement=True, is_pinned=True,
            )
        )
        db.session.add(
            Message(
                sender_id=instructor.id, recipient_id=students[0].id,
                course_id=course.id, subject="Your report",
                body="Please revise section 3 and resubmit.",
            )
        )

        db.session.commit()
        print(f"  seeded: {course.code}, {plain.code}, {len(students)} students, "
              f"{len(questions)} questions, 5 attempts")


# --------------------------------------------------------------------------- #

def routes_for(client, role_label):
    """Every GET route, with ids filled in from the seeded data."""
    with app.app_context():
        course = Course.query.filter_by(code="ME 412").first()
        plain = Course.query.filter_by(code="ME 321").first()
        exam = Exam.query.filter_by(title="Quiz 3 — Ideal Gases").first()
        attempt = Attempt.query.filter_by(exam_id=exam.id).first()
        item = GradeItem.query.filter_by(title="Technical Report").first()
        quiz_item = GradeItem.query.filter_by(title="Quiz 1 — Pure Substances").first()
        rubric = Rubric.query.filter_by(name="Technical Report (ME 412)").first()
        bank = QuestionBank.query.first()
        student = User.query.filter_by(role=ROLE_STUDENT).first()
        instructor = User.query.filter_by(role=ROLE_INSTRUCTOR).first()
        section = Section.query.filter_by(course_id=course.id).first()
        message = Message.query.first()
        submission = Submission.query.first()

        common = [
            "/", "/healthz", "/manifest.webmanifest", "/service-worker.js",
            "/offline", "/api/clock", "/api/katex/check",
            "/messages/", "/messages/compose", f"/messages/{message.id}",
            "/password",
        ]

        staff = [
            "/subjects/", f"/subjects/{course.id}",
            f"/subjects/{course.id}?section={section.id}",
            f"/subjects/{course.id}/enrol",
            f"/subjects/{course.id}/class-list",
            f"/subjects/{course.id}/class-list?section={section.id}",
            f"/subjects/{course.id}/attendance",
            f"/subjects/{course.id}/attendance?section={section.id}",
            "/subjects/students",
            f"/subjects/{plain.id}",
            "/exams/", "/exams/new", f"/exams/new?course={course.id}",
            f"/exams/{exam.id}", f"/exams/{exam.id}/monitor",
            f"/exams/{exam.id}/results", f"/exams/{exam.id}/analysis",
            f"/exams/attempts/{attempt.id}",
            f"/exams/attempts/{attempt.id}/report",
            f"/exams/attempts/{attempt.id}/report?key=0&why=0",
            f"/exams/{exam.id}/reports",
            f"/exams/{exam.id}/reports?submitted=0",
            f"/reports/{course.id}/mark-record",
            f"/reports/{course.id}/mark-record?section={section.id}",
            f"/reports/{course.id}/mark-record/{student.id}",
            f"/reports/{course.id}/mark-record/{student.id}?rubrics=0&attendance=0",
            f"/reports/{course.id}/report-of-grades",
            f"/reports/{course.id}/report-of-grades?section={section.id}&rows=15",
            f"/exams/{exam.id}/export.csv",
            f"/exams/{exam.id}/questions.csv",
            f"/exams/{exam.id}/preview",
            f"/exams/{exam.id}/preview?order=shuffled&seed=3",
            f"/exams/{exam.id}/paper",
            f"/exams/{exam.id}/paper?key=1&sets=3&shuffle=1&space=wide",
            f"/exams/{exam.id}/paper?sheet=0&space=tight",
            "/exams/questions/template.csv",
            f"/exams/banks/{bank.id}/export.csv",
            f"/exams/course/{course.id}/export.csv",
            f"/exams/course/{course.id}/items.json",
            f"/exams/course/{course.id}/sections.json",
            f"/api/exam/{exam.id}/monitor.json",
            "/exams/banks", f"/exams/banks/{bank.id}",
            "/assistant/",
            "/gradebook/", f"/gradebook/{course.id}",
            f"/gradebook/{course.id}?scope=laboratory",
            f"/gradebook/{course.id}?section={section.id}",
            f"/gradebook/item/{quiz_item.id}",
            f"/gradebook/item/{item.id}",
            f"/gradebook/{course.id}/removal",
            f"/gradebook/{course.id}/finalise",
            f"/gradebook/{course.id}/student/{student.id}",
            f"/gradebook/{course.id}/export.csv",
            "/rubrics/", f"/rubrics/{rubric.id}",
            f"/rubrics/grade/item/{item.id}",
            f"/reports/{course.id}/gradesheet",
            f"/reports/{course.id}/gradesheet?detail=1",
            f"/reports/{course.id}/gradesheet?section={section.id}",
            f"/reports/{course.id}/class-list/print",
            f"/reports/{course.id}/slip/{student.id}",
            f"/reports/{course.id}/attendance/print",
            f"/reports/{course.id}/summary",
        ]

        admin = [
            "/admin/", "/admin/term", "/admin/users",
            "/admin/users?role=student", "/admin/users?q=surname",
            "/admin/users/new", f"/admin/users/{student.id}",
            f"/admin/users/{instructor.id}",
            "/admin/subjects", "/admin/system", "/admin/audit",
            "/admin/assistant", "/admin/password-prompts",
            "/admin/password-prompts?pending=0",
        ]

        student_routes = [
            "/take/", f"/take/{exam.id}/start",
            "/my/progress", f"/my/progress?subject={course.id}",
            "/my/history", "/my/account",
            f"/my/submit/{item.id}",
        ]

    if role_label == "superadmin":
        return common + staff + admin
    if role_label == "instructor":
        return common + staff
    return common + student_routes


def sign_in(client, identifier, password):
    response = client.post(
        "/login", data={"identifier": identifier, "password": password},
        follow_redirects=False,
    )
    return response.status_code in (302, 303)


def walk(role_label, identifier, password):
    failures = []
    with app.test_client() as client:
        if not sign_in(client, identifier, password):
            return [(f"LOGIN as {identifier}", "could not sign in", "")]

        for path in routes_for(client, role_label):
            try:
                response = client.get(path, follow_redirects=False)
            except Exception:
                failures.append((path, "exception", _describe(traceback.format_exc())))
                continue

            if response.status_code >= 500:
                body = response.get_data(as_text=True)
                failures.append((path, response.status_code, _describe(body)))
            elif response.status_code == 404:
                failures.append((path, 404, "route or record missing"))
    return failures


def post_checks():
    """Exercise the write paths that matter most."""
    problems = []

    with app.app_context():
        course = Course.query.filter_by(code="ME 412").first()
        exam = Exam.query.filter_by(title="Quiz 3 — Ideal Gases").first()
        quiz_item = GradeItem.query.filter_by(title="Quiz 1 — Pure Substances").first()
        course_id, exam_id, item_id = course.id, exam.id, quiz_item.id
        students = [s.id for s in course.roster()][:3]
        multipart_count = Question.query.filter_by(
            exam_id=exam.id, qtype=Q_MULTIPART
        ).count()

    with app.test_client() as client:
        sign_in(client, "rabrasaldo", "teach123")

        # The multi-part parser that used to crash on a type word in the
        # numeric position -- both orders must work.
        for bulk in [
            "d | Find the final pressure | 350 | num | 4 | 2",
            "e | Find the final pressure | 350 | 4 | num | 2",
            "f | Name the process | isobaric | short | 2",
            "g | Broken line with no answer",
        ]:
            response = client.post(
                f"/exams/{exam_id}/questions",
                data={
                    "qtype": "multipart",
                    "text": "Smoke-test multi-part problem",
                    "points": "1",
                    "parts_bulk": bulk,
                },
                follow_redirects=True,
            )
            if response.status_code >= 500:
                problems.append((f"POST multipart ({bulk[:24]}…)", response.status_code,
                                 _describe(response.get_data(as_text=True))))

        # Saving marks.
        data = {f"score_{sid}": "20" for sid in students}
        response = client.post(f"/gradebook/item/{item_id}", data=data,
                               follow_redirects=True)
        if response.status_code >= 500:
            problems.append(("POST scores", response.status_code,
                             _describe(response.get_data(as_text=True))))

        # An out-of-range mark must be refused, not stored.
        response = client.post(f"/gradebook/item/{item_id}",
                               data={f"score_{students[0]}": "9999"},
                               follow_redirects=True)
        body = response.get_data(as_text=True)
        if "above the maximum" not in body:
            problems.append(("POST out-of-range mark", "not rejected", body[-500:]))

        # Component weights must total 100.
        response = client.post(f"/subjects/{course_id}/components",
                               data={"weight_1": "10"}, follow_redirects=True)
        if response.status_code >= 500:
            problems.append(("POST components", response.status_code, ""))

        # Finalise, then reopen.
        response = client.post(f"/gradebook/{course_id}/finalise",
                               data={"confirm": "FINALISE"}, follow_redirects=True)
        if response.status_code >= 500:
            problems.append(("POST finalise", response.status_code,
                             _describe(response.get_data(as_text=True))))
        response = client.post(f"/gradebook/{course_id}/reopen", follow_redirects=True)
        if response.status_code >= 500:
            problems.append(("POST reopen", response.status_code, ""))

        # Rescore.
        response = client.post(f"/exams/{exam_id}/rescore", follow_redirects=True)
        if response.status_code >= 500:
            problems.append(("POST rescore", response.status_code, ""))

    # A student sitting the exam: start, autosave, submit.
    with app.test_client() as client:
        with app.app_context():
            fresh = (
                User.query.filter_by(role=ROLE_STUDENT)
                .order_by(User.id.desc())
                .all()
            )
            target = None
            for candidate in fresh:
                if Attempt.query.filter_by(
                    exam_id=exam_id, student_id=candidate.id
                ).first() is None and Enrollment.query.filter_by(
                    course_id=course_id, student_id=candidate.id, is_dropped=False
                ).first() is not None:
                    target = candidate
                    break
            number = target.student_number if target else None

        if number:
            sign_in(client, number, number)
            response = client.post(f"/take/{exam_id}/start",
                                   data={"access_code": "ME412A"},
                                   follow_redirects=False)
            if response.status_code >= 500:
                problems.append(("POST start exam", response.status_code,
                                 _describe(response.get_data(as_text=True))))
            else:
                with app.app_context():
                    attempt = Attempt.query.filter_by(
                        exam_id=exam_id, student_id=target.id
                    ).first()
                    attempt_id = attempt.id if attempt else None
                    first_q = (
                        Question.query.filter_by(exam_id=exam_id, qtype=Q_NUMERIC)
                        .first()
                    )
                    qid = first_q.id if first_q else None

                if attempt_id:
                    response = client.get(f"/take/attempt/{attempt_id}")
                    if response.status_code >= 500:
                        problems.append(("GET sit", response.status_code,
                                         _describe(response.get_data(as_text=True))))

                    response = client.post(f"/take/attempt/{attempt_id}/autosave",
                                           data={f"q{qid}": "202.65"})
                    body = response.get_data(as_text=True).replace(" ", "").lower()
                    if response.status_code >= 500 or '"ok":true' not in body:
                        problems.append(("POST autosave", response.status_code,
                                         _describe(response.get_data(as_text=True))))

                    response = client.post(f"/take/attempt/{attempt_id}/event",
                                           data={"kind": "screen_sleep",
                                                 "detail": "display off"})
                    if response.status_code >= 500:
                        problems.append(("POST event", response.status_code, ""))

                    response = client.post(f"/take/attempt/{attempt_id}/submit",
                                           data={f"q{qid}": "202.65"},
                                           follow_redirects=True)
                    if response.status_code >= 500:
                        problems.append(("POST submit", response.status_code,
                                         _describe(response.get_data(as_text=True))))

                    response = client.get(f"/take/attempt/{attempt_id}/review")
                    if response.status_code >= 500:
                        problems.append(("GET review", response.status_code,
                                         _describe(response.get_data(as_text=True))))

    # Backup round trip.
    with app.test_client() as client:
        sign_in(client, "admin", "admin123")
        response = client.post("/admin/system/backup", follow_redirects=False)
        if response.status_code >= 500:
            problems.append(("POST backup", response.status_code, ""))

    return problems


def grading_checks():
    """Arithmetic the whole gradebook depends on."""
    from app.grading import bump_one_step, transmute

    problems = []
    cases = [
        (100.0, "1.00"), (95.60, "1.00"), (95.59, "1.25"), (91.15, "1.25"),
        (86.70, "1.50"), (82.25, "1.75"), (77.80, "2.00"), (73.35, "2.25"),
        (68.90, "2.50"), (64.45, "2.75"), (60.00, "3.00"), (59.99, "5.00"),
        (55.00, "5.00"), (0.0, "5.00"),
    ]
    for percent, expected in cases:
        got = transmute(percent)
        if got != expected:
            problems.append((f"transmute({percent})", got, f"expected {expected}"))

    for grade, expected in [("2.50", "2.25"), ("1.00", "1.00"), ("3.00", "2.75"),
                            ("5.00", "5.00")]:
        got = bump_one_step(grade)
        if got != expected:
            problems.append((f"bump({grade})", got, f"expected {expected}"))

    from app.marking import mark_numeric, mark_short, parse_number

    numeric = [
        ("202.6", "202.65", 2.0, True),
        ("202.65", "202.65", 0.0, True),
        ("210", "202.65", 2.0, False),
        ("1,162", "1162", 1.0, True),
        ("202.65 kPa", "202.65", 1.0, True),
        ("2.0265e2", "202.65", 1.0, True),
        ("", "202.65", 5.0, False),
        ("abc", "202.65", 5.0, False),
    ]
    for response, key, tolerance, expected in numeric:
        got = mark_numeric(response, key, tolerance)
        if got != expected:
            problems.append((f"mark_numeric({response!r})", got, f"expected {expected}"))

    short = [
        ("boyle's law", "Boyle's law | Boyles law", True),
        ("BOYLES LAW", "Boyle's law | Boyles law", True),
        ("Charles law", "Boyle's law | Boyles law", False),
        ("", "Boyle", False),
    ]
    for response, key, expected in short:
        got = mark_short(response, key)
        if got != expected:
            problems.append((f"mark_short({response!r})", got, f"expected {expected}"))

    if parse_number("3 x 10^3") != 3000.0:
        problems.append(("parse_number('3 x 10^3')", parse_number("3 x 10^3"), "expected 3000"))

    return problems


# --------------------------------------------------------------------------- #

def main():
    print("Seeding…")
    seed()

    print("\nGrading arithmetic…")
    problems = grading_checks()
    if problems:
        for name, got, note in problems:
            print(f"  FAIL {name}: got {got!r} — {note}")
    else:
        print("  all transmutation, bumping and marking cases pass")

    total = list(problems)

    for role, identifier, password in [
        ("superadmin", "admin", "admin123"),
        ("instructor", "rabrasaldo", "teach123"),
        ("student", "2023-0001", "2023-0001"),
    ]:
        print(f"\nWalking routes as {role}…")
        failures = walk(role, identifier, password)
        if failures:
            for path, status, body in failures:
                print(f"  FAIL {path} -> {status}  {body}")
            total.extend(failures)
        else:
            count = len(routes_for(None, role))
            print(f"  {count} routes OK")

    print("\nWrite paths…")
    failures = post_checks()
    if failures:
        for name, status, body in failures:
            print(f"  FAIL {name} -> {status}  {_describe(str(body)) if body else ''}")
        total.extend(failures)
    else:
        print("  all write paths OK")

    print("\n" + "=" * 62)
    if total:
        print(f"{len(total)} problem(s) found")
        return 1
    print("Everything passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
