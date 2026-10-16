"""Schema reconciliation.

The previous build kept a hand-written dictionary of "columns to add", and a
duplicated key in that dictionary silently dropped three real columns -- a bug
that only showed up in production as a 500.  This module removes the hand
list: it reads the *model metadata* as the single source of truth, asks the
live database what it actually has, and adds whatever is missing.

It runs before ``create_all()`` and before any ORM query, because the failure
that cost the most time was an ORM ``SELECT`` touching a column that the
migration had not yet added -- on PostgreSQL that poisons the transaction and
every later statement fails too.

Works on both SQLite and PostgreSQL; types and defaults are rendered through
the live dialect's own compiler rather than string-patched.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from .extensions import db

log = logging.getLogger(__name__)


def _existing_columns(inspector, table: str) -> set[str]:
    try:
        return {c["name"] for c in inspector.get_columns(table)}
    except Exception:
        return set()


def _render_type(column) -> str:
    dialect = db.engine.dialect
    try:
        return column.type.compile(dialect=dialect)
    except Exception:
        # Last-resort mapping for an exotic type.
        return "TEXT"


def _render_default(column) -> str:
    """A literal DEFAULT clause, when one can be rendered safely.

    SQLite cannot add a NOT NULL column without a default, and existing rows
    need a sensible value either way.
    """
    default = column.default
    if default is None or getattr(default, "is_callable", False):
        # Python-side callables (e.g. utcnow) cannot be a SQL default; fall
        # back to a neutral literal so existing rows are valid.
        if not column.nullable:
            type_name = _render_type(column).upper()
            if "CHAR" in type_name or "TEXT" in type_name:
                return "''"
            if "BOOL" in type_name:
                return "FALSE" if db.engine.name != "sqlite" else "0"
            if any(t in type_name for t in ("INT", "FLOAT", "NUMERIC", "REAL", "DOUBLE")):
                return "0"
        return ""

    value = getattr(default, "arg", None)
    if callable(value):
        return ""
    if isinstance(value, bool):
        if db.engine.name == "sqlite":
            return "1" if value else "0"
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return ""


def reconcile_schema(app) -> dict:
    """Add any column present in the models but missing in the database.

    Returns a report the diagnostics page can show, so the instructor can see
    the schema state without needing a developer.
    """
    report: dict = {"dialect": db.engine.name, "added": [], "skipped": [], "errors": []}
    inspector = inspect(db.engine)

    try:
        live_tables = set(inspector.get_table_names())
    except SQLAlchemyError as exc:
        report["errors"].append(f"cannot inspect database: {exc}")
        return report

    for table in db.metadata.sorted_tables:
        if table.name not in live_tables:
            # create_all() will build it in full; nothing to patch.
            report["skipped"].append(f"{table.name} (new table)")
            continue

        have = _existing_columns(inspector, table.name)
        for column in table.columns:
            if column.name in have:
                continue
            if column.primary_key:
                # Cannot retro-fit a primary key; surface it instead of failing.
                report["errors"].append(
                    f"{table.name}.{column.name} is a missing primary key — rebuild needed"
                )
                continue

            ddl = f'"{column.name}" {_render_type(column)}'
            literal = _render_default(column)
            if literal:
                ddl += f" DEFAULT {literal}"

            statement = f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}'
            try:
                db.session.execute(text(statement))
                db.session.commit()
                report["added"].append(f"{table.name}.{column.name}")
                log.info("migration: added %s.%s", table.name, column.name)
            except SQLAlchemyError as exc:
                # A concurrent worker may have added it a moment ago; that is
                # fine.  Anything else is recorded, never raised, so one odd
                # column cannot stop the app from booting.
                db.session.rollback()
                message = str(getattr(exc, "orig", exc)).strip().splitlines()[0]
                if "exist" in message.lower():
                    report["skipped"].append(f"{table.name}.{column.name} (already present)")
                else:
                    report["errors"].append(f"{table.name}.{column.name}: {message}")

    return report


def ensure_indexes(app) -> None:
    """Create the composite indexes the hot queries rely on."""
    statements = [
        'CREATE INDEX IF NOT EXISTS ix_enrollment_course_section '
        'ON enrollment (course_id, section_id)',
        'CREATE INDEX IF NOT EXISTS ix_score_item_student ON score (item_id, student_id)',
        'CREATE INDEX IF NOT EXISTS ix_attempt_exam_student ON attempt (exam_id, student_id)',
        'CREATE INDEX IF NOT EXISTS ix_answer_attempt ON answer (attempt_id)',
        'CREATE INDEX IF NOT EXISTS ix_course_term ON course (school_year, semester)',
    ]
    for statement in statements:
        try:
            db.session.execute(text(statement))
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()


def initialise(app) -> dict:
    """Full boot sequence for the database, in the order that matters."""
    with app.app_context():
        # 1. Patch existing tables FIRST, while no ORM query has run yet.
        report = reconcile_schema(app)
        # 2. Create anything entirely new.
        try:
            db.metadata.create_all(bind=db.engine)
        except SQLAlchemyError as exc:
            db.session.rollback()
            report["errors"].append(f"create_all: {exc}")
        # 3. A second pass catches columns on tables that step 2 just created
        #    from an older partial definition.
        follow_up = reconcile_schema(app)
        report["added"] += follow_up["added"]
        report["errors"] += follow_up["errors"]
        # 4. Indexes last, once every column exists.
        ensure_indexes(app)
        app.config["SCHEMA_REPORT"] = report
    return report
