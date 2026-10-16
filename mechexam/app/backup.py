"""Backup and restore.

Losing a term's grades to a redeploy is the worst thing this app could do, and
it nearly happened once already.  So: a one-click JSON export of every table
that an instructor can download and keep, and a restore that reads it back.

The export is dialect-neutral (plain JSON, ISO dates), so a SQLite database
from PythonAnywhere restores into PostgreSQL on Railway and the other way
round.  Password hashes are included so accounts keep working; the file is
therefore sensitive and the UI says so.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path

from flask import current_app
from sqlalchemy import inspect

from .extensions import db

log = logging.getLogger(__name__)

# Restore order matters: parents before children.
TABLE_ORDER = [
    "setting",
    "user",
    "student_year_status",
    "course",
    "section",
    "enrollment",
    "rubric",
    "rubric_criterion",
    "grade_component",
    "grade_item",
    "score",
    "final_grade",
    "question_bank",
    "exam",
    "question",
    "sub_question",
    "attempt",
    "answer",
    "integrity_event",
    "rubric_score",
    "submission",
    "message",
    "message_read",
    "attendance_session",
    "attendance_record",
    "audit_log",
]


def _encode(value):
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def _decode(value, column):
    if value is None:
        return None
    python_type = None
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        python_type = None

    if python_type is _dt.datetime and isinstance(value, str):
        return _dt.datetime.fromisoformat(value)
    if python_type is _dt.date and isinstance(value, str):
        return _dt.date.fromisoformat(value[:10])
    if python_type is bool and isinstance(value, (int, str)):
        return str(value).lower() in ("1", "true", "yes", "t")
    return value


def build_snapshot() -> dict:
    """Every row of every table, as plain JSON-able data."""
    inspector = inspect(db.engine)
    live = set(inspector.get_table_names())
    payload: dict = {
        "format": "mechexam-backup",
        "version": 2,
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "dialect": db.engine.name,
        "tables": {},
    }

    for table in db.metadata.sorted_tables:
        if table.name not in live:
            continue
        rows = db.session.execute(table.select()).mappings().all()
        payload["tables"][table.name] = [
            {key: _encode(value) for key, value in row.items()} for row in rows
        ]
    payload["counts"] = {name: len(rows) for name, rows in payload["tables"].items()}
    return payload


def export_backup(directory: Path | None = None) -> Path:
    directory = directory or Path(current_app.config["BACKUP_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"mechexam-backup-{stamp}.json"
    path.write_text(json.dumps(build_snapshot(), indent=1, ensure_ascii=False), "utf-8")
    return path


def restore_snapshot(payload: dict, wipe: bool = True) -> dict:
    """Load a snapshot back into the database.

    With ``wipe`` the existing rows are cleared first (children before
    parents), which is what a real restore needs.  Without it, rows are
    inserted and anything that collides is skipped, which is useful for
    merging one subject's data into a live system.
    """
    if payload.get("format") != "mechexam-backup":
        raise ValueError("Not a portal backup file.")

    tables = {t.name: t for t in db.metadata.sorted_tables}
    inspector = inspect(db.engine)
    live = set(inspector.get_table_names())
    report = {"restored": {}, "skipped": [], "errors": []}

    if wipe:
        for name in reversed(TABLE_ORDER):
            table = tables.get(name)
            if table is None or name not in live:
                continue
            try:
                db.session.execute(table.delete())
            except Exception as exc:
                db.session.rollback()
                report["errors"].append(f"clearing {name}: {exc}")
        db.session.commit()

    for name in TABLE_ORDER:
        table = tables.get(name)
        if table is None or name not in live:
            continue
        rows = payload["tables"].get(name) or []
        if not rows:
            continue

        columns = {c.name: c for c in table.columns}
        inserted = 0
        for raw in rows:
            data = {
                key: _decode(value, columns[key])
                for key, value in raw.items()
                if key in columns
            }
            if not data:
                continue
            try:
                db.session.execute(table.insert().values(**data))
                inserted += 1
            except Exception as exc:
                db.session.rollback()
                if wipe:
                    report["errors"].append(
                        f"{name} id={raw.get('id')}: "
                        f"{str(getattr(exc, 'orig', exc)).splitlines()[0]}"
                    )
                else:
                    report["skipped"].append(f"{name} id={raw.get('id')}")
                continue
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            report["errors"].append(f"committing {name}: {exc}")
        report["restored"][name] = inserted

    _fix_sequences(report)
    return report


def _fix_sequences(report: dict) -> None:
    """PostgreSQL only: move each identity sequence past the restored ids.

    Without this, the next insert collides with a restored row and the user
    sees a duplicate-key error that looks like data corruption.
    """
    if db.engine.name != "postgresql":
        return
    from sqlalchemy import text

    for table in db.metadata.sorted_tables:
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or pk[0].name != "id":
            continue
        statement = (
            f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM \"{table.name}\"), 1), true)"
        )
        try:
            db.session.execute(text(statement))
            db.session.commit()
        except Exception:
            db.session.rollback()
            report["errors"].append(f"sequence for {table.name} not reset")


def list_backups() -> list[dict]:
    directory = Path(current_app.config["BACKUP_DIR"])
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob("mechexam-backup-*.json"), reverse=True):
        stat = path.stat()
        out.append(
            {
                "name": path.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "created": _dt.datetime.fromtimestamp(stat.st_mtime),
            }
        )
    return out
