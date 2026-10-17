"""Database models.

Design notes
------------
*   JSON-shaped values are stored in ``Text`` columns with explicit
    ``json.dumps``/``loads`` helpers rather than a native JSON type, so the
    same schema works on SQLite (laptop, PythonAnywhere) and PostgreSQL
    (Railway) with no dialect branching.
*   Nothing cascades destructively by accident: records a teacher would be
    upset to lose (scores, attempts, final grades) are deleted only through
    explicit code paths.
*   Every table that belongs to an academic term carries ``school_year`` and
    ``semester`` so history survives the end-of-term rollover.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _loads(raw, default):
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if value is not None else default


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

class Setting(db.Model):
    """Application-wide key/value settings (school year, semester, defaults)."""

    __tablename__ = "setting"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    DEFAULTS = {
        "school_year": "2026-2027",
        "semester": "First Semester",
        "removal_low": "55.00",
        "removal_high": "59.99",
        "passing_percent": "60.00",
        "default_averaging": "total_points",
        "lecture_share": "67",
        "laboratory_share": "33",
        "allow_student_self_password": "1",
        "grades_released_default": "0",
        "exam_autosave_seconds": "20",
        "exam_clock_poll_seconds": "60",
    }

    # Bounds for the exam-timing settings. The upper limit on autosave is a
    # safety floor, not a preference: at 120 seconds a student whose battery
    # dies loses at most two minutes of work, and that is as much as this app
    # is willing to risk however tempting the saving in server time.
    LIMITS = {
        "exam_autosave_seconds": (5, 120),
        "exam_clock_poll_seconds": (15, 300),
    }

    @classmethod
    def get_bounded(cls, key: str) -> int:
        low, high = cls.LIMITS.get(key, (0, 10 ** 6))
        return max(low, min(high, cls.get_int(key, low)))

    @classmethod
    def get(cls, key: str, default: str | None = None) -> str:
        row = db.session.get(cls, key)
        if row is not None and row.value is not None:
            return row.value
        if default is not None:
            return default
        return cls.DEFAULTS.get(key, "")

    @classmethod
    def get_int(cls, key: str, default: int = 0) -> int:
        try:
            return int(float(cls.get(key)))
        except (TypeError, ValueError):
            return default

    @classmethod
    def get_float(cls, key: str, default: float = 0.0) -> float:
        try:
            return float(cls.get(key))
        except (TypeError, ValueError):
            return default

    @classmethod
    def get_bool(cls, key: str, default: bool = False) -> bool:
        raw = cls.get(key)
        if raw == "":
            return default
        return raw not in ("0", "false", "False", "no", "")

    @classmethod
    def put(cls, key: str, value) -> None:
        row = db.session.get(cls, key)
        if row is None:
            row = cls(key=key)
            db.session.add(row)
        row.value = "" if value is None else str(value)

    @classmethod
    def term(cls) -> tuple[str, str]:
        return cls.get("school_year"), cls.get("semester")


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #

ROLE_SUPERADMIN = "superadmin"
ROLE_INSTRUCTOR = "instructor"
ROLE_STUDENT = "student"
ROLES = (ROLE_SUPERADMIN, ROLE_INSTRUCTOR, ROLE_STUDENT)


class User(UserMixin, db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    student_number = db.Column(db.String(40), unique=True, index=True)
    email = db.Column(db.String(160))
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(20), nullable=False, default=ROLE_STUDENT, index=True)

    name = db.Column(db.String(160), nullable=False, default="")
    program = db.Column(db.String(80), default="")          # e.g. BSME
    year_level = db.Column(db.String(20), default="")
    contact = db.Column(db.String(60), default="")

    rank = db.Column(db.String(80), default="")             # for instructors
    is_enabled = db.Column(db.Boolean, nullable=False, default=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    reset_code = db.Column(db.String(32))
    theme = db.Column(db.String(16), default="system")

    created_at = db.Column(db.DateTime, default=utcnow)
    last_login_at = db.Column(db.DateTime)

    # ---- password -------------------------------------------------------- #
    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, raw)

    def issue_reset_code(self) -> str:
        code = secrets.token_hex(3).upper()
        self.reset_code = code
        self.must_change_password = True
        return code

    # ---- flask-login ----------------------------------------------------- #
    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return bool(self.is_enabled)

    # ---- roles ----------------------------------------------------------- #
    @property
    def is_superadmin(self) -> bool:
        return self.role == ROLE_SUPERADMIN

    @property
    def is_instructor(self) -> bool:
        return self.role == ROLE_INSTRUCTOR

    @property
    def is_student(self) -> bool:
        return self.role == ROLE_STUDENT

    @property
    def is_staff(self) -> bool:
        return self.role in (ROLE_SUPERADMIN, ROLE_INSTRUCTOR)

    @property
    def display_name(self) -> str:
        return self.name or self.username

    @property
    def sort_name(self) -> str:
        return (self.name or self.username).strip().upper()

    def __repr__(self) -> str:
        return f"<User {self.username} {self.role}>"


@login_manager.user_loader
def _load_user(user_id: str):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


class StudentYearStatus(db.Model):
    """Whole-year standing: shifted course, transferred out, stopped.

    A non-active status removes the student from every class list for that
    school year without touching the grades already recorded.
    """

    __tablename__ = "student_year_status"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    school_year = db.Column(db.String(20), nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False, default="active")
    note = db.Column(db.String(255), default="")
    recorded_at = db.Column(db.DateTime, default=utcnow)

    student = db.relationship("User", backref=db.backref("year_statuses", lazy="dynamic"))

    STATUSES = ("active", "shifted", "transferred", "stopped")
    LABELS = {
        "active": "Active",
        "shifted": "Shifted course",
        "transferred": "Transferred to another school",
        "stopped": "Stopped",
    }

    __table_args__ = (db.UniqueConstraint("student_id", "school_year", name="uq_year_status"),)

    @property
    def label(self) -> str:
        return self.LABELS.get(self.status, self.status.title())


# --------------------------------------------------------------------------- #
# Subjects, sections, enrolment
# --------------------------------------------------------------------------- #

class Course(db.Model):
    __tablename__ = "course"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")

    units = db.Column(db.Float, default=3.0)
    lec_units = db.Column(db.Float, default=3.0)
    lab_units = db.Column(db.Float, default=0.0)
    has_laboratory = db.Column(db.Boolean, nullable=False, default=False)

    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    school_year = db.Column(db.String(20), nullable=False, index=True)
    semester = db.Column(db.String(30), nullable=False, index=True)

    # Grading behaviour
    averaging_method = db.Column(db.String(20), default="total_points")
    lecture_share = db.Column(db.Float, default=67.0)
    laboratory_share = db.Column(db.Float, default=33.0)
    removal_low = db.Column(db.Float, default=55.0)
    removal_high = db.Column(db.Float, default=59.99)

    grades_released = db.Column(db.Boolean, nullable=False, default=False)
    is_finalised = db.Column(db.Boolean, nullable=False, default=False)
    finalised_at = db.Column(db.DateTime)
    removal_open = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime, default=utcnow)

    owner = db.relationship("User", foreign_keys=[owner_id])
    sections = db.relationship(
        "Section", backref="course", cascade="all, delete-orphan",
        order_by="Section.name", lazy="selectin",
    )
    components = db.relationship(
        "GradeComponent", backref="course", cascade="all, delete-orphan",
        order_by="GradeComponent.order_index", lazy="selectin",
    )

    @property
    def label(self) -> str:
        return f"{self.code} — {self.title}"

    @property
    def term_label(self) -> str:
        return f"{self.semester}, {self.school_year}"

    def active_enrollments(self, section_id: int | None = None):
        q = (
            Enrollment.query.filter_by(course_id=self.id, is_dropped=False)
            .join(User, Enrollment.student_id == User.id)
            .order_by(User.name)
        )
        if section_id:
            q = q.filter(Enrollment.section_id == section_id)
        return q

    def roster(self, section_id: int | None = None) -> list["User"]:
        """Students who count for this subject right now.

        Excludes per-subject drops and anyone whose whole-year standing has
        been set to shifted / transferred / stopped.
        """
        blocked = {
            s.student_id
            for s in StudentYearStatus.query.filter(
                StudentYearStatus.school_year == self.school_year,
                StudentYearStatus.status != "active",
            )
        }
        out = []
        for enr in self.active_enrollments(section_id).all():
            if enr.student_id in blocked:
                continue
            if enr.student is not None:
                out.append(enr.student)
        out.sort(key=lambda u: u.sort_name)
        return out

    def __repr__(self) -> str:
        return f"<Course {self.code} {self.school_year} {self.semester}>"


class Section(db.Model):
    __tablename__ = "section"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False, index=True)
    name = db.Column(db.String(60), nullable=False)
    schedule = db.Column(db.String(160), default="")
    room = db.Column(db.String(80), default="")
    capacity = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)

    @property
    def headcount(self) -> int:
        return Enrollment.query.filter_by(
            course_id=self.course_id, section_id=self.id, is_dropped=False
        ).count()


class Enrollment(db.Model):
    __tablename__ = "enrollment"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), index=True)

    is_dropped = db.Column(db.Boolean, nullable=False, default=False)
    dropped_at = db.Column(db.DateTime)
    drop_note = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=utcnow)

    student = db.relationship("User", foreign_keys=[student_id], lazy="joined")
    section = db.relationship("Section", foreign_keys=[section_id], lazy="joined")
    course = db.relationship("Course", foreign_keys=[course_id])

    __table_args__ = (
        db.UniqueConstraint("course_id", "student_id", name="uq_enrollment"),
    )


# --------------------------------------------------------------------------- #
# Gradebook structure
# --------------------------------------------------------------------------- #

SCOPE_LECTURE = "lecture"
SCOPE_LABORATORY = "laboratory"


class GradeComponent(db.Model):
    """Written Works / Performance Task / Major Examinations, per scope."""

    __tablename__ = "grade_component"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    scope = db.Column(db.String(20), nullable=False, default=SCOPE_LECTURE)
    weight_percent = db.Column(db.Float, nullable=False, default=0.0)
    order_index = db.Column(db.Integer, default=0)
    kind = db.Column(db.String(30), default="written")   # written | performance | major

    items = db.relationship(
        "GradeItem", backref="component", cascade="all, delete-orphan",
        order_by="GradeItem.order_index", lazy="selectin",
    )

    # CET default weighting, from the college grading sheet.
    DEFAULTS = {
        SCOPE_LECTURE: [
            ("Written Works", 20.0, "written"),
            ("Performance Task", 40.0, "performance"),
            ("Major Examinations", 40.0, "major"),
        ],
        SCOPE_LABORATORY: [
            ("Written Works", 20.0, "written"),
            ("Performance Task", 60.0, "performance"),
            ("Major Examinations", 20.0, "major"),
        ],
    }


class GradeItem(db.Model):
    """One quiz, task, report or major exam inside a component."""

    __tablename__ = "grade_item"

    id = db.Column(db.Integer, primary_key=True)
    component_id = db.Column(db.Integer, db.ForeignKey("grade_component.id"), nullable=False, index=True)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), index=True)

    title = db.Column(db.String(200), nullable=False)
    item_type = db.Column(db.String(40), default="quiz")
    max_points = db.Column(db.Float, nullable=False, default=100.0)
    weight_multiplier = db.Column(db.Float, nullable=False, default=1.0)
    date_given = db.Column(db.Date)

    rubric_id = db.Column(db.Integer, db.ForeignKey("rubric.id"))
    drive_folder_url = db.Column(db.String(500), default="")
    accepts_submission = db.Column(db.Boolean, nullable=False, default=False)
    instructions = db.Column(db.Text, default="")

    is_published = db.Column(db.Boolean, nullable=False, default=True)
    order_index = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)

    rubric = db.relationship("Rubric", foreign_keys=[rubric_id])
    scores = db.relationship(
        "Score", backref="item", cascade="all, delete-orphan", lazy="selectin"
    )

    TYPES = (
        ("quiz", "Quiz"),
        ("long_exam", "Long Examination"),
        ("seatwork", "Seatwork"),
        ("assignment", "Assignment"),
        ("recitation", "Recitation"),
        ("task", "Performance Task"),
        ("video", "Video Presentation"),
        ("report", "Technical Report"),
        ("lab_report", "Laboratory Report"),
        ("project", "Project"),
        ("midterm", "Midterm Examination"),
        ("final", "Final Examination"),
        ("removal", "Removal Examination"),
    )

    @property
    def type_label(self) -> str:
        return dict(self.TYPES).get(self.item_type, self.item_type.replace("_", " ").title())


class Score(db.Model):
    __tablename__ = "score"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("grade_item.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)

    points = db.Column(db.Float)
    is_override = db.Column(db.Boolean, nullable=False, default=False)
    override_note = db.Column(db.String(255), default="")
    remarks = db.Column(db.String(255), default="")
    graded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    student = db.relationship("User", foreign_keys=[student_id])

    __table_args__ = (db.UniqueConstraint("item_id", "student_id", name="uq_score"),)


class FinalGrade(db.Model):
    """Computed result, frozen when the instructor finalises the subject."""

    __tablename__ = "final_grade"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)

    lecture_percent = db.Column(db.Float)
    laboratory_percent = db.Column(db.Float)
    final_percent = db.Column(db.Float)
    grade = db.Column(db.String(10))
    remark = db.Column(db.String(40))

    removal_status = db.Column(db.String(20), default="")   # qualified|passed|failed
    removal_percent = db.Column(db.Float)
    pre_removal_grade = db.Column(db.String(10))
    was_bumped = db.Column(db.Boolean, nullable=False, default=False)

    finalised_at = db.Column(db.DateTime)
    note = db.Column(db.String(255), default="")

    student = db.relationship("User", foreign_keys=[student_id])

    __table_args__ = (db.UniqueConstraint("course_id", "student_id", name="uq_final_grade"),)


# --------------------------------------------------------------------------- #
# Rubrics
# --------------------------------------------------------------------------- #

class Rubric(db.Model):
    __tablename__ = "rubric"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), index=True)
    name = db.Column(db.String(160), nullable=False)
    kind = db.Column(db.String(40), default="custom")
    description = db.Column(db.Text, default="")
    points_per_criterion = db.Column(db.Float, nullable=False, default=10.0)
    is_template = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    criteria = db.relationship(
        "RubricCriterion", backref="rubric", cascade="all, delete-orphan",
        order_by="RubricCriterion.order_index", lazy="selectin",
    )

    KINDS = (
        ("essay", "Essay / Long Answer"),
        ("video", "Video Presentation"),
        ("report", "Technical Report"),
        ("lab_report", "Laboratory Report"),
        ("oral", "Oral Reporting"),
        ("project", "Design Project"),
        ("custom", "Custom"),
    )

    @property
    def total_points(self) -> float:
        return sum((c.max_points or 0.0) for c in self.criteria)


class RubricCriterion(db.Model):
    __tablename__ = "rubric_criterion"

    id = db.Column(db.Integer, primary_key=True)
    rubric_id = db.Column(db.Integer, db.ForeignKey("rubric.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    max_points = db.Column(db.Float, nullable=False, default=10.0)
    order_index = db.Column(db.Integer, default=0)
    levels_raw = db.Column("levels_json", db.Text)

    @property
    def levels(self) -> list:
        return _loads(self.levels_raw, [])

    @levels.setter
    def levels(self, value) -> None:
        self.levels_raw = _dumps(value or [])


class RubricScore(db.Model):
    __tablename__ = "rubric_score"

    id = db.Column(db.Integer, primary_key=True)
    rubric_id = db.Column(db.Integer, db.ForeignKey("rubric.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("grade_item.id"), index=True)
    answer_id = db.Column(db.Integer, db.ForeignKey("answer.id"), index=True)

    scores_raw = db.Column("scores_json", db.Text)
    total = db.Column(db.Float, default=0.0)
    comment = db.Column(db.Text, default="")
    graded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    rubric = db.relationship("Rubric", foreign_keys=[rubric_id])

    @property
    def scores(self) -> dict:
        return _loads(self.scores_raw, {})

    @scores.setter
    def scores(self, value) -> None:
        self.scores_raw = _dumps(value or {})


# --------------------------------------------------------------------------- #
# Exams
# --------------------------------------------------------------------------- #

EXIT_STRICT = "strict"
EXIT_GRACE = "grace"
EXIT_OFF = "off"


class Exam(db.Model):
    __tablename__ = "exam"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False, index=True)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), index=True)

    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(60), default="Quiz")
    topic = db.Column(db.String(160), default="")
    instructions = db.Column(db.Text, default="")

    duration_minutes = db.Column(db.Integer, nullable=False, default=60)
    is_open = db.Column(db.Boolean, nullable=False, default=False)
    opened_at = db.Column(db.DateTime)
    closes_at = db.Column(db.DateTime)
    closed_at = db.Column(db.DateTime)
    access_code = db.Column(db.String(40))

    shuffle_questions = db.Column(db.Boolean, nullable=False, default=True)
    shuffle_options = db.Column(db.Boolean, nullable=False, default=True)
    exit_mode = db.Column(db.String(20), nullable=False, default=EXIT_STRICT)
    exit_grace_seconds = db.Column(db.Integer, nullable=False, default=180)
    block_paste = db.Column(db.Boolean, nullable=False, default=True)
    allow_calculator = db.Column(db.Boolean, nullable=False, default=True)
    allow_backtrack = db.Column(db.Boolean, nullable=False, default=True)
    one_question_per_page = db.Column(db.Boolean, nullable=False, default=False)

    show_score = db.Column(db.Boolean, nullable=False, default=True)
    show_answers = db.Column(db.Boolean, nullable=False, default=False)

    bank_draw_count = db.Column(db.Integer, default=0)
    link_item_id = db.Column(db.Integer, db.ForeignKey("grade_item.id"))

    school_year = db.Column(db.String(20), index=True)
    semester = db.Column(db.String(30), index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    course = db.relationship("Course", foreign_keys=[course_id])
    section = db.relationship("Section", foreign_keys=[section_id])
    link_item = db.relationship("GradeItem", foreign_keys=[link_item_id])
    questions = db.relationship(
        "Question",
        primaryjoin="and_(Question.exam_id==Exam.id)",
        backref="exam", cascade="all, delete-orphan",
        order_by="Question.order_index", lazy="selectin",
    )

    CATEGORIES = (
        "Quiz", "Long Examination", "Seatwork", "Assignment",
        "Midterm Examination", "Final Examination", "Removal Examination",
        "Laboratory Exercise", "Practice Set",
    )

    EXIT_MODES = (
        (EXIT_STRICT, "Submit at once when the student genuinely leaves"),
        (EXIT_GRACE, "Warn, then submit if they do not return in time"),
        (EXIT_OFF, "Only record the event, never auto-submit"),
    )

    @property
    def total_points(self) -> float:
        return sum(q.effective_points for q in self.questions)

    @property
    def seconds_remaining(self) -> int:
        """Shared clock: starts when the instructor opens the exam."""
        if not self.is_open or not self.opened_at:
            return 0
        deadline = self.closes_at
        if deadline is None:
            return 0
        return max(0, int((deadline - utcnow()).total_seconds()))

    @property
    def is_expired(self) -> bool:
        return bool(self.is_open and self.closes_at and utcnow() >= self.closes_at)


class QuestionBank(db.Model):
    __tablename__ = "question_bank"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utcnow)

    course = db.relationship("Course", foreign_keys=[course_id])

    @property
    def question_count(self) -> int:
        return Question.query.filter_by(bank_id=self.id).count()


Q_MCQ = "mcq"
Q_TRUEFALSE = "truefalse"
Q_NUMERIC = "numeric"
Q_SHORT = "short"
Q_MATCHING = "matching"
Q_ESSAY = "essay"
Q_MULTIPART = "multipart"


class Question(db.Model):
    """A question belongs either to an exam or to a reusable bank."""

    __tablename__ = "question"

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id"), index=True)
    bank_id = db.Column(db.Integer, db.ForeignKey("question_bank.id"), index=True)

    qtype = db.Column(db.String(20), nullable=False, default=Q_MCQ)
    text = db.Column(db.Text, nullable=False, default="")
    options_raw = db.Column("options_json", db.Text)
    answer = db.Column(db.Text, default="")
    points = db.Column(db.Float, nullable=False, default=1.0)
    tolerance_percent = db.Column(db.Float, default=0.0)
    unit = db.Column(db.String(40), default="")

    figure_path = db.Column(db.String(300))
    figure_caption = db.Column(db.String(300), default="")

    rubric_id = db.Column(db.Integer, db.ForeignKey("rubric.id"))
    explanation = db.Column(db.Text, default="")
    topic = db.Column(db.String(160), default="")
    difficulty = db.Column(db.String(20), default="moderate")
    order_index = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow)

    rubric = db.relationship("Rubric", foreign_keys=[rubric_id])
    parts = db.relationship(
        "SubQuestion", backref="question", cascade="all, delete-orphan",
        order_by="SubQuestion.order_index", lazy="selectin",
    )

    TYPES = (
        (Q_MCQ, "Multiple Choice"),
        (Q_TRUEFALSE, "True or False"),
        (Q_NUMERIC, "Numerical Answer"),
        (Q_SHORT, "Short Answer"),
        (Q_MATCHING, "Matching Type"),
        (Q_ESSAY, "Essay / Long Answer"),
        (Q_MULTIPART, "Problem Solving (multi-part)"),
    )
    DIFFICULTIES = ("easy", "moderate", "hard")

    @property
    def type_label(self) -> str:
        return dict(self.TYPES).get(self.qtype, self.qtype)

    @property
    def options(self) -> list:
        return _loads(self.options_raw, [])

    @options.setter
    def options(self, value) -> None:
        self.options_raw = _dumps(value or [])

    @property
    def pairs(self) -> list:
        """Matching type: [{'left': ..., 'right': ...}, ...]"""
        return _loads(self.options_raw, [])

    @pairs.setter
    def pairs(self, value) -> None:
        self.options_raw = _dumps(value or [])

    @property
    def effective_points(self) -> float:
        if self.qtype == Q_MULTIPART:
            total = sum((p.points or 0.0) for p in self.parts)
            return total if total else (self.points or 0.0)
        if self.qtype == Q_MATCHING:
            n = len(self.pairs)
            per_pair = self.points if self.points is not None else 1.0
            return per_pair * n if n else 0.0
        return self.points or 0.0

    @property
    def is_auto_gradable(self) -> bool:
        return self.qtype != Q_ESSAY


class SubQuestion(db.Model):
    """One lettered item under a multi-part problem."""

    __tablename__ = "sub_question"

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey("question.id"), nullable=False, index=True)
    label = db.Column(db.String(12), default="")
    text = db.Column(db.Text, default="")
    answer_type = db.Column(db.String(20), default=Q_NUMERIC)
    answer = db.Column(db.Text, default="")
    options_raw = db.Column("options_json", db.Text)
    points = db.Column(db.Float, nullable=False, default=1.0)
    tolerance_percent = db.Column(db.Float, default=0.0)
    unit = db.Column(db.String(40), default="")
    order_index = db.Column(db.Integer, default=0)

    @property
    def options(self) -> list:
        return _loads(self.options_raw, [])

    @options.setter
    def options(self, value) -> None:
        self.options_raw = _dumps(value or [])


# --------------------------------------------------------------------------- #
# Attempts and answers
# --------------------------------------------------------------------------- #

ATTEMPT_IN_PROGRESS = "in_progress"
ATTEMPT_SUBMITTED = "submitted"


class Attempt(db.Model):
    __tablename__ = "attempt"

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)

    status = db.Column(db.String(20), nullable=False, default=ATTEMPT_IN_PROGRESS)
    started_at = db.Column(db.DateTime, default=utcnow)
    submitted_at = db.Column(db.DateTime)
    last_seen_at = db.Column(db.DateTime)
    auto_submitted = db.Column(db.Boolean, nullable=False, default=False)
    submit_reason = db.Column(db.String(60), default="")

    score = db.Column(db.Float, default=0.0)
    total_points = db.Column(db.Float, default=0.0)
    manual_pending = db.Column(db.Integer, default=0)

    is_override = db.Column(db.Boolean, nullable=False, default=False)
    override_score = db.Column(db.Float)
    override_note = db.Column(db.String(255), default="")

    seed = db.Column(db.Integer, default=0)
    question_order_raw = db.Column("question_order", db.Text)
    reopen_count = db.Column(db.Integer, default=0)
    extra_seconds = db.Column(db.Integer, default=0)

    exam = db.relationship("Exam", foreign_keys=[exam_id])
    student = db.relationship("User", foreign_keys=[student_id], lazy="joined")
    answers = db.relationship(
        "Answer", backref="attempt", cascade="all, delete-orphan", lazy="selectin"
    )
    events = db.relationship(
        "IntegrityEvent", backref="attempt", cascade="all, delete-orphan",
        order_by="IntegrityEvent.created_at", lazy="selectin",
    )

    __table_args__ = (db.UniqueConstraint("exam_id", "student_id", name="uq_attempt"),)

    @property
    def question_order(self) -> list:
        return _loads(self.question_order_raw, [])

    @question_order.setter
    def question_order(self, value) -> None:
        self.question_order_raw = _dumps(value or [])

    @property
    def effective_score(self) -> float:
        if self.is_override and self.override_score is not None:
            return self.override_score
        return self.score or 0.0

    @property
    def percent(self) -> float:
        total = self.total_points or 0.0
        if total <= 0:
            return 0.0
        return round(self.effective_score / total * 100.0, 2)

    @property
    def is_submitted(self) -> bool:
        return self.status == ATTEMPT_SUBMITTED


class Answer(db.Model):
    __tablename__ = "answer"

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempt.id"), nullable=False, index=True)
    question_id = db.Column(db.Integer, db.ForeignKey("question.id"), nullable=False, index=True)
    sub_question_id = db.Column(db.Integer, db.ForeignKey("sub_question.id"), index=True)

    response = db.Column(db.Text, default="")
    is_correct = db.Column(db.Boolean)
    points_awarded = db.Column(db.Float, default=0.0)
    max_points = db.Column(db.Float, default=0.0)
    needs_manual = db.Column(db.Boolean, nullable=False, default=False)

    feedback = db.Column(db.Text, default="")
    graded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    graded_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    question = db.relationship("Question", foreign_keys=[question_id], lazy="joined")
    sub_question = db.relationship("SubQuestion", foreign_keys=[sub_question_id])

    __table_args__ = (
        db.UniqueConstraint("attempt_id", "question_id", "sub_question_id", name="uq_answer"),
    )


class IntegrityEvent(db.Model):
    """Focus losses, tab switches, paste attempts -- visible to the instructor."""

    __tablename__ = "integrity_event"

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempt.id"), nullable=False, index=True)
    kind = db.Column(db.String(40), nullable=False)
    detail = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=utcnow)

    LABELS = {
        "blur": "Left the exam window",
        "hidden": "Switched away / minimised",
        "screen_sleep": "Screen went to sleep (tolerated)",
        "return": "Returned to the exam",
        "paste": "Paste attempt blocked",
        "copy": "Copy attempt",
        "auto_submit": "Auto-submitted",
        "resume": "Resumed after reconnect",
        "fullscreen_exit": "Left full screen",
    }

    @property
    def label(self) -> str:
        return self.LABELS.get(self.kind, self.kind.replace("_", " ").title())


# --------------------------------------------------------------------------- #
# Submissions, messaging, attendance, audit
# --------------------------------------------------------------------------- #

class Submission(db.Model):
    """A student's file for a performance task (local copy and/or Drive)."""

    __tablename__ = "submission"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("grade_item.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)

    filename = db.Column(db.String(300), default="")
    stored_path = db.Column(db.String(500), default="")
    mime_type = db.Column(db.String(120), default="")
    size_bytes = db.Column(db.Integer, default=0)

    drive_file_id = db.Column(db.String(120))
    drive_url = db.Column(db.String(500))
    drive_error = db.Column(db.String(500), default="")

    note = db.Column(db.Text, default="")
    submitted_at = db.Column(db.DateTime, default=utcnow)

    student = db.relationship("User", foreign_keys=[student_id], lazy="joined")
    item = db.relationship("GradeItem", foreign_keys=[item_id])

    @property
    def is_video(self) -> bool:
        return (self.mime_type or "").startswith("video/")

    @property
    def is_image(self) -> bool:
        return (self.mime_type or "").startswith("image/")


class Message(db.Model):
    __tablename__ = "message"

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), index=True)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), index=True)

    subject = db.Column(db.String(200), default="")
    body = db.Column(db.Text, default="")
    is_announcement = db.Column(db.Boolean, nullable=False, default=False)
    is_pinned = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    sender = db.relationship("User", foreign_keys=[sender_id], lazy="joined")
    recipient = db.relationship("User", foreign_keys=[recipient_id])
    course = db.relationship("Course", foreign_keys=[course_id])


class MessageRead(db.Model):
    __tablename__ = "message_read"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("message.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    read_at = db.Column(db.DateTime, default=utcnow)

    __table_args__ = (db.UniqueConstraint("message_id", "user_id", name="uq_message_read"),)


class AttendanceSession(db.Model):
    __tablename__ = "attendance_session"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False, index=True)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), index=True)
    meeting_date = db.Column(db.Date, nullable=False)
    topic = db.Column(db.String(200), default="")
    created_at = db.Column(db.DateTime, default=utcnow)

    records = db.relationship(
        "AttendanceRecord", backref="session", cascade="all, delete-orphan", lazy="selectin"
    )
    section = db.relationship("Section", foreign_keys=[section_id])


class AttendanceRecord(db.Model):
    __tablename__ = "attendance_record"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("attendance_session.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="present")
    note = db.Column(db.String(200), default="")

    STATUSES = (("present", "Present"), ("late", "Late"),
                ("excused", "Excused"), ("absent", "Absent"))

    __table_args__ = (db.UniqueConstraint("session_id", "student_id", name="uq_attendance"),)


class AuditLog(db.Model):
    """Who changed what, when -- grade edits above all."""

    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    action = db.Column(db.String(60), nullable=False)
    entity = db.Column(db.String(60), default="")
    entity_id = db.Column(db.Integer)
    detail = db.Column(db.Text, default="")
    ip_address = db.Column(db.String(60), default="")
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    user = db.relationship("User", foreign_keys=[user_id])


def log_action(user, action: str, entity: str = "", entity_id=None, detail: str = "") -> None:
    from flask import has_request_context, request

    try:
        entry = AuditLog(
            user_id=getattr(user, "id", None),
            action=action,
            entity=entity,
            entity_id=entity_id,
            detail=detail[:2000] if detail else "",
            ip_address=request.remote_addr if has_request_context() else "",
        )
        db.session.add(entry)
    except Exception:
        db.session.rollback()
