"""First-boot seeding: settings defaults, a superadmin, rubric templates.

Idempotent -- running it on every boot is safe and is what keeps a fresh
deployment usable without a shell.
"""
from __future__ import annotations

import logging
import os

from .extensions import db
from .models import (
    ROLE_SUPERADMIN,
    Rubric,
    RubricCriterion,
    Setting,
    User,
)

log = logging.getLogger(__name__)


RUBRIC_TEMPLATES = {
    "video": (
        "Video Presentation",
        [
            ("Technical content and accuracy",
             "Principles are correct, computations are sound, units are consistent."),
            ("Organisation and flow",
             "Clear introduction, logical development, definite conclusion."),
            ("Delivery and articulation",
             "Audible, well paced, speaks to the audience rather than reading."),
            ("Visual aids and production quality",
             "Readable slides and figures, steady footage, clear audio."),
            ("Time management",
             "Keeps within the allotted time and covers all required points."),
        ],
    ),
    "report": (
        "Technical Report",
        [
            ("Abstract and introduction",
             "States the problem, scope and objectives concisely."),
            ("Methodology",
             "Procedure is reproducible; assumptions are stated."),
            ("Results and computations",
             "Correct analysis, properly labelled tables and figures, correct units."),
            ("Discussion and conclusion",
             "Interprets results, compares with theory, draws warranted conclusions."),
            ("Format, grammar and references",
             "Follows the prescribed format; cites sources properly."),
        ],
    ),
    "lab_report": (
        "Laboratory Report",
        [
            ("Objectives and theoretical background",
             "States what the exercise sets out to show and the governing relations."),
            ("Apparatus and procedure",
             "Equipment listed, steps recorded accurately."),
            ("Data and computations",
             "Readings tabulated, sample computation shown, units correct."),
            ("Analysis of results",
             "Graphs and error analysis; deviations explained."),
            ("Conclusion and housekeeping",
             "Conclusions follow from the data; report submitted neatly and on time."),
        ],
    ),
    "oral": (
        "Oral Reporting",
        [
            ("Mastery of subject matter", "Answers questions confidently and correctly."),
            ("Organisation of ideas", "Coherent sequence, clear transitions."),
            ("Language and delivery", "Correct terminology, audible and engaging."),
            ("Visual support", "Relevant, legible, not read verbatim."),
            ("Audience engagement", "Handles questions and holds attention."),
        ],
    ),
    "essay": (
        "Essay / Long Answer",
        [
            ("Correctness of concepts", "Engineering principles applied correctly."),
            ("Depth of reasoning", "Argument is developed, not merely asserted."),
            ("Use of evidence and computation", "Supports claims with data or analysis."),
            ("Organisation", "Clear structure from premise to conclusion."),
            ("Clarity of expression", "Precise, readable technical English."),
        ],
    ),
    "project": (
        "Design Project",
        [
            ("Problem definition and constraints", "Scope, standards and limits identified."),
            ("Design methodology", "Systematic selection and sizing, justified choices."),
            ("Computations and drawings", "Accurate analysis; clear, dimensioned drawings."),
            ("Feasibility and safety", "Costing, materials and safety factors considered."),
            ("Documentation and defence", "Complete report; design defended convincingly."),
        ],
    ),
}


def bootstrap(app) -> None:
    with app.app_context():
        try:
            _seed_settings()
            _seed_superadmin(app)
            _seed_rubric_templates()
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("seeding failed; the app will still start")


def _seed_settings() -> None:
    for key, value in Setting.DEFAULTS.items():
        if db.session.get(Setting, key) is None:
            db.session.add(Setting(key=key, value=value))


def _seed_superadmin(app) -> None:
    if User.query.filter_by(role=ROLE_SUPERADMIN).first() is not None:
        return

    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "admin123")
    name = os.environ.get("ADMIN_NAME", "System Administrator")

    user = User(
        username=username,
        role=ROLE_SUPERADMIN,
        name=name,
        must_change_password=(password == "admin123"),
    )
    user.set_password(password)
    db.session.add(user)
    log.info("created superadmin %r", username)


def _seed_rubric_templates() -> None:
    for kind, (name, criteria) in RUBRIC_TEMPLATES.items():
        existing = Rubric.query.filter_by(kind=kind, is_template=True).first()
        if existing is not None:
            continue
        rubric = Rubric(
            course_id=None,
            name=name,
            kind=kind,
            is_template=True,
            points_per_criterion=10.0,
            description="Standard template — 10 points per criterion. "
                        "Copy it into a subject to edit.",
        )
        db.session.add(rubric)
        db.session.flush()
        for index, (title, description) in enumerate(criteria, start=1):
            db.session.add(
                RubricCriterion(
                    rubric_id=rubric.id,
                    title=title,
                    description=description,
                    max_points=10.0,
                    order_index=index,
                    levels_raw=None,
                )
            )
