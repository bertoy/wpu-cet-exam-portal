"""The gradebook: entering marks, overriding, finalising.

Note on naming: result objects expose ``entries``, never ``items``.  In Jinja,
``component.items`` would resolve to the dict's own ``.items`` method and
silently render a bound method instead of the data -- a bug that cost real
time in the previous build.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user


from ..extensions import db
from ..grading import (
    class_tally,
    compute_class,
    compute_student,
    finalise_course,
    removal_candidates,
    removal_item,
    reopen_course,
)
from ..models import (
    Course,
    GradeComponent,
    GradeItem,
    Rubric,
    SCOPE_LABORATORY,
    SCOPE_LECTURE,
    Score,
    Setting,
    User,
    log_action,
)
from ..security import require_course, staff_required, visible_courses

bp = Blueprint("gradebook", __name__)


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

@bp.route("/")
@staff_required
def index():
    courses = visible_courses().all()
    return render_template("gradebook/index.html", courses=courses)


@bp.route("/<int:course_id>")
@staff_required
def course(course_id: int):
    """The grid: students down, activities across, computed standing at the end."""
    course_row = require_course(course_id, manage=False)
    section_id = request.args.get("section", type=int)
    scope = request.args.get("scope", SCOPE_LECTURE)
    if scope not in (SCOPE_LECTURE, SCOPE_LABORATORY):
        scope = SCOPE_LECTURE

    results = compute_class(course_row, section_id)
    components = [c for c in course_row.components if c.scope == scope]
    components.sort(key=lambda c: (c.order_index or 0, c.id))

    return render_template(
        "gradebook/course.html",
        course=course_row,
        results=results,
        components=components,
        scope=scope,
        sections=list(course_row.sections),
        section_id=section_id,
        tally=class_tally(results),
        has_laboratory=course_row.has_laboratory,
        removal=removal_item(course_row),
        candidates=len([r for r in results if r.qualifies_for_removal]),
    )


# --------------------------------------------------------------------------- #
# The class record, for printing
# --------------------------------------------------------------------------- #

def _paginate_columns(components, budget: int) -> list[list[dict]]:
    """Pack the activity columns into sheets that fit across the paper.

    A term's gradebook is wider than any sheet of paper: eight chapter
    exercises, four quizzes and two examinations is already two dozen columns,
    and shrinking them to fit makes the figures unreadable, which defeats the
    point of printing them.

    So the columns are split across sheets instead. A component is kept whole
    wherever it fits, because reading half of Performance Task on one sheet and
    half on another is worse than turning a page. A component too wide for a
    sheet on its own is split, and its percentage follows the last part.
    """
    pages: list[list[dict]] = []
    current: list[dict] = []
    used = 0

    for component in components:
        items = list(component.items)
        # One column per activity, plus one for the component's own percentage.
        width = len(items) + 1

        if width <= budget - used:
            current.append({"component": component, "activities": items,
                            "show_percent": True, "continued": False})
            used += width
            continue

        if current and width <= budget:
            pages.append(current)
            current = [{"component": component, "activities": items,
                        "show_percent": True, "continued": False}]
            used = width
            continue

        # Wider than a whole sheet: split it, and keep the percentage with the
        # last piece so it is never read as a partial total.
        offset = 0
        while offset < len(items):
            room = budget - used
            if room < 2:
                pages.append(current)
                current = []
                used = 0
                room = budget
            chunk = items[offset:offset + max(1, room - 1)]
            offset += len(chunk)
            last = offset >= len(items)
            current.append({
                "component": component,
                # Named "activities", not "items": in Jinja a dict's ``.items``
                # resolves to the dict's own method and silently renders a
                # bound method instead of the data. This file's own docstring
                # records that costing real time once already.
                "activities": chunk,
                "show_percent": last,
                "continued": not last or chunk is not items[:len(chunk)],
            })
            used += len(chunk) + (1 if last else 0)
            if not last:
                pages.append(current)
                current = []
                used = 0

    if current:
        pages.append(current)
    return pages or [[]]


@bp.route("/<int:course_id>/record")
@staff_required
def class_record(course_id: int):
    """The whole class's marks on paper: students down, activities across.

    This is the gradebook grid as a printable sheet. It is the working record
    an instructor keeps in a folder and brings to a meeting, which is why it
    shows every activity rather than only the totals the registrar wants.
    """
    course_row = require_course(course_id, manage=False)
    section_id = request.args.get("section", type=int)
    scope = request.args.get("scope", SCOPE_LECTURE)
    if scope not in (SCOPE_LECTURE, SCOPE_LABORATORY):
        scope = SCOPE_LECTURE

    results = compute_class(course_row, section_id)
    components = [c for c in course_row.components if c.scope == scope]
    components.sort(key=lambda c: (c.order_index or 0, c.id))

    # Chosen so a sheet stays readable rather than to fit the most in.
    columns_per_sheet = max(6, min(request.args.get("cols", type=int) or 16, 30))

    column_pages = _paginate_columns(components, columns_per_sheet)

    # Each student's marks keyed by activity, so a sheet showing an arbitrary
    # slice of the columns can look up the right cell without the template
    # walking the component list for every cell it draws.
    lines = []
    for result in results:
        scope_result = (
            result.laboratory if scope == SCOPE_LABORATORY else result.lecture
        )
        marks = {}
        percents = {}
        if scope_result:
            for component_result in scope_result.components:
                percents[component_result.component.id] = component_result.raw_percent
                for entry in component_result.entries:
                    marks[entry.item.id] = entry
        lines.append({
            "result": result,
            "scope_result": scope_result,
            "marks": marks,
            "percents": percents,
        })



    # The percentage a component is worth within this scope, keyed by id, so
    # the template does not have to reach back through the component list.
    section = next((s for s in course_row.sections if s.id == section_id), None)

    # The rows are not split here. The column headings are a table header
    # group, so the browser repeats them when a long class list runs over, and
    # letting it decide where to break beats guessing a row count that is
    # wrong as soon as a name wraps onto two lines.
    sheets = [
        {
            "rows": lines,
            "groups": groups,
            "part": (
                f"part {index + 1} of {len(column_pages)}"
                if len(column_pages) > 1 else ""
            ),
            # The standing columns repeat on every part so that any one sheet
            # can be read on its own.
            "show_summary": True,
        }
        for index, groups in enumerate(column_pages)
    ]

    return render_template(
        "gradebook/class_record.html",
        course=course_row,
        scope=scope,
        results=results,
        sheets=sheets,
        section=section,
        sections=list(course_row.sections),
        section_id=section_id,
        tally=class_tally(results),
        columns_per_sheet=columns_per_sheet,
        printed_on=datetime.now(),
    )


# --------------------------------------------------------------------------- #
# Activities
# --------------------------------------------------------------------------- #

def _require_item(item_id: int) -> GradeItem:
    item = GradeItem.query.get(item_id)
    if item is None:
        abort(404)
    component = GradeComponent.query.get(item.component_id)
    if component is None:
        abort(404)
    require_course(component.course_id)
    return item


@bp.route("/item/<int:item_id>", methods=["GET", "POST"])
@staff_required
def item(item_id: int):
    """Enter or edit the marks for one activity."""
    item_row = _require_item(item_id)
    component = item_row.component
    course_row = component.course
    section_id = request.args.get("section", type=int) or item_row.section_id

    if request.method == "POST":
        return _save_scores(item_row, course_row, section_id)

    students = course_row.roster(section_id)
    scores = {
        row.student_id: row
        for row in Score.query.filter_by(item_id=item_row.id).all()
    }

    entered = [s for s in scores.values() if s.points is not None]
    stats = {}
    if entered and item_row.max_points:
        values = [s.points for s in entered]
        percents = [v / item_row.max_points * 100 for v in values]
        stats = {
            "entered": len(entered),
            "of": len(students),
            "mean": round(sum(values) / len(values), 2),
            "mean_percent": round(sum(percents) / len(percents), 2),
            "high": max(values),
            "low": min(values),
            "passed": sum(1 for p in percents if p >= 60),
        }

    from sqlalchemy import or_ as _or

    rubrics = (
        Rubric.query.filter(
            _or(Rubric.course_id == course_row.id, Rubric.is_template.is_(True))
        )
        .order_by(Rubric.is_template, Rubric.name)
        .all()
    )

    return render_template(
        "gradebook/item.html",
        item=item_row,
        component=component,
        course=course_row,
        students=students,
        scores=scores,
        rubrics=rubrics,
        sections=list(course_row.sections),
        section_id=section_id,
        stats=stats,
    )


def _save_scores(item_row: GradeItem, course_row: Course, section_id):
    changed = 0
    problems: list[str] = []
    existing = {
        row.student_id: row for row in Score.query.filter_by(item_id=item_row.id).all()
    }

    for student in course_row.roster(section_id):
        field = f"score_{student.id}"
        if field not in request.form:
            continue

        raw = (request.form.get(field) or "").strip()
        row = existing.get(student.id)

        if raw == "":
            # Blank clears the mark rather than recording a zero, so an
            # in-progress gradebook stays honest.
            if row is not None and row.points is not None:
                row.points = None
                changed += 1
            continue

        try:
            points = float(raw)
        except ValueError:
            problems.append(f"{student.display_name}: “{raw}” is not a number")
            continue

        if points < 0:
            problems.append(f"{student.display_name}: a mark cannot be negative")
            continue
        if item_row.max_points and points > item_row.max_points:
            problems.append(
                f"{student.display_name}: {points:g} is above the maximum of "
                f"{item_row.max_points:g}"
            )
            continue

        if row is None:
            row = Score(item_id=item_row.id, student_id=student.id)
            db.session.add(row)
            existing[student.id] = row

        if row.points != points:
            changed += 1
        row.points = points
        row.remarks = (request.form.get(f"remarks_{student.id}") or "").strip()
        row.graded_by_id = current_user.id

        if request.form.get(f"override_{student.id}"):
            row.is_override = True
            row.override_note = (
                request.form.get(f"note_{student.id}") or "Entered by instructor"
            ).strip()
        elif row.is_override and not request.form.get(f"keep_override_{student.id}"):
            row.is_override = False
            row.override_note = ""

    log_action(
        current_user, "scores_saved", "grade_item", item_row.id,
        f"{changed} changed",
    )
    db.session.commit()

    if changed:
        flash(f"Saved {changed} mark(s).", "success")
    else:
        flash("No marks changed.", "info")
    if problems:
        flash("Not saved: " + "; ".join(problems[:6]), "error")

    return redirect(
        url_for("gradebook.item", item_id=item_row.id, section=section_id)
    )


@bp.route("/item/<int:item_id>/settings", methods=["POST"])
@staff_required
def item_settings(item_id: int):
    item_row = _require_item(item_id)
    form = request.form

    if form.get("action") == "delete":
        course_id = item_row.component.course_id
        label = item_row.title
        entered = Score.query.filter(
            Score.item_id == item_row.id, Score.points.isnot(None)
        ).count()
        if entered and form.get("confirm") != "DELETE":
            flash(
                f"“{label}” has {entered} mark(s) recorded. Type DELETE to "
                f"confirm.",
                "error",
            )
            return redirect(url_for("gradebook.item", item_id=item_row.id))
        db.session.delete(item_row)
        log_action(current_user, "item_deleted", "grade_item", item_id, label)
        db.session.commit()
        flash(f"Deleted “{label}”.", "success")
        return redirect(url_for("gradebook.course", course_id=course_id))

    new_max = form.get("max_points", type=float)
    if new_max and new_max > 0 and new_max != item_row.max_points:
        over = Score.query.filter(
            Score.item_id == item_row.id, Score.points > new_max
        ).count()
        if over:
            flash(
                f"{over} recorded mark(s) are above {new_max:g}. "
                f"Lower those first, or set a higher maximum.",
                "error",
            )
            return redirect(url_for("gradebook.item", item_id=item_row.id))
        item_row.max_points = new_max

    item_row.title = (form.get("title") or item_row.title).strip()
    item_row.item_type = form.get("item_type") or item_row.item_type
    item_row.weight_multiplier = form.get("weight_multiplier", type=float) or 1.0
    item_row.instructions = (form.get("instructions") or "").strip()
    item_row.drive_folder_url = (form.get("drive_folder_url") or "").strip()
    item_row.accepts_submission = bool(form.get("accepts_submission"))
    item_row.is_published = bool(form.get("is_published"))
    item_row.section_id = form.get("section_id", type=int) or None

    # Attaching a shared template copies it into the subject first, so editing
    # the rubric here cannot change what every other instructor sees.
    rubric_id = form.get("rubric_id", type=int) or None
    if rubric_id is not None:
        rubric = Rubric.query.get(rubric_id)
        if rubric is None:
            rubric_id = None
        elif rubric.is_template:
            from .courses import _copy_template

            rubric_id = _copy_template(rubric, item_row.component.course).id
    item_row.rubric_id = rubric_id

    raw_date = form.get("date_given")
    if raw_date:
        try:
            item_row.date_given = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    component_id = form.get("component_id", type=int)
    if component_id:
        target = GradeComponent.query.filter_by(
            id=component_id, course_id=item_row.component.course_id
        ).first()
        if target is not None:
            item_row.component_id = target.id

    log_action(current_user, "item_settings", "grade_item", item_row.id)
    db.session.commit()
    flash("Activity settings saved.", "success")
    return redirect(url_for("gradebook.item", item_id=item_row.id))


@bp.route("/item/<int:item_id>/import", methods=["POST"])
@staff_required
def item_import(item_id: int):
    """Paste ``student number, score`` lines -- faster than clicking through."""
    item_row = _require_item(item_id)
    course_row = item_row.component.course

    raw = request.form.get("pasted") or ""
    upload = request.files.get("csv_file")
    if upload is not None and upload.filename:
        raw = upload.read().decode("utf-8-sig", "replace")
    if not raw.strip():
        flash("Paste the marks, or choose a CSV file.", "error")
        return redirect(url_for("gradebook.item", item_id=item_row.id))

    roster = {
        (s.student_number or "").lower(): s for s in course_row.roster() if s.student_number
    }
    by_name = {s.name.strip().lower(): s for s in course_row.roster()}

    saved = 0
    problems: list[str] = []
    existing = {
        row.student_id: row for row in Score.query.filter_by(item_id=item_row.id).all()
    }

    for line_no, row in enumerate(csv.reader(io.StringIO(raw.replace("\t", ","))), 1):
        cells = [c.strip() for c in row if c and c.strip()]
        if len(cells) < 2:
            continue
        key, value = cells[0], cells[-1]
        student = roster.get(key.lower()) or by_name.get(key.lower())
        if student is None:
            problems.append(f"line {line_no}: no student matching “{key}”")
            continue
        try:
            points = float(value)
        except ValueError:
            problems.append(f"line {line_no}: “{value}” is not a number")
            continue
        if points < 0 or (item_row.max_points and points > item_row.max_points):
            problems.append(f"line {line_no}: {points:g} is out of range")
            continue

        score_row = existing.get(student.id)
        if score_row is None:
            score_row = Score(item_id=item_row.id, student_id=student.id)
            db.session.add(score_row)
            existing[student.id] = score_row
        score_row.points = points
        score_row.graded_by_id = current_user.id
        saved += 1

    log_action(current_user, "scores_imported", "grade_item", item_row.id, f"{saved}")
    db.session.commit()
    flash(f"Imported {saved} mark(s).", "success")
    if problems:
        flash("Skipped: " + "; ".join(problems[:6]), "warning")
    return redirect(url_for("gradebook.item", item_id=item_row.id))


# --------------------------------------------------------------------------- #
# Removal examination
# --------------------------------------------------------------------------- #

@bp.route("/<int:course_id>/removal", methods=["GET", "POST"])
@staff_required
def removal(course_id: int):
    """The subject's own removal-examination area.

    Candidates are determined from the *computed final* standing, which is why
    this page only makes sense once the term's marks are in -- and why the
    result is applied when the subject is finalised, not before.
    """
    course_row = require_course(course_id)
    item = removal_item(course_row)

    if request.method == "POST":
        action = request.form.get("action")

        if action == "create":
            if item is not None:
                flash("This subject already has a removal examination.", "info")
                return redirect(url_for("gradebook.removal", course_id=course_row.id))

            scope = request.form.get("scope", SCOPE_LECTURE)
            component = next(
                (
                    c
                    for c in course_row.components
                    if c.scope == scope and c.kind == "major"
                ),
                None,
            ) or next((c for c in course_row.components if c.scope == scope), None)
            if component is None:
                flash("Set up the grading components first.", "error")
                return redirect(url_for("gradebook.removal", course_id=course_row.id))

            item = GradeItem(
                component_id=component.id,
                title="Removal Examination",
                item_type="removal",
                max_points=request.form.get("max_points", type=float) or 100.0,
                weight_multiplier=0.0,     # scored separately, not averaged in
                is_published=False,
                order_index=999,
            )
            db.session.add(item)
            course_row.removal_open = True
            log_action(current_user, "removal_created", "course", course_row.id)
            db.session.commit()
            flash(
                "A removal examination has been created. Record the marks here, "
                "then finalise the subject to apply the results.",
                "success",
            )
            return redirect(url_for("gradebook.removal", course_id=course_row.id))

        if action == "save" and item is not None:
            existing = {
                row.student_id: row
                for row in Score.query.filter_by(item_id=item.id).all()
            }
            saved = 0
            for result in removal_candidates(course_row):
                field = f"score_{result.student.id}"
                if field not in request.form:
                    continue
                raw = (request.form.get(field) or "").strip()
                row = existing.get(result.student.id)
                if raw == "":
                    if row is not None:
                        row.points = None
                    continue
                try:
                    points = float(raw)
                except ValueError:
                    continue
                if row is None:
                    row = Score(item_id=item.id, student_id=result.student.id)
                    db.session.add(row)
                    existing[result.student.id] = row
                row.points = points
                row.graded_by_id = current_user.id
                saved += 1

            log_action(current_user, "removal_scores", "grade_item", item.id, str(saved))
            db.session.commit()
            flash(f"Saved {saved} removal mark(s).", "success")
            return redirect(url_for("gradebook.removal", course_id=course_row.id))

        if action == "delete" and item is not None:
            db.session.delete(item)
            course_row.removal_open = False
            db.session.commit()
            flash("The removal examination has been removed.", "success")
            return redirect(url_for("gradebook.removal", course_id=course_row.id))

    candidates = removal_candidates(course_row)
    scores = {}
    if item is not None:
        scores = {
            row.student_id: row for row in Score.query.filter_by(item_id=item.id).all()
        }

    all_results = compute_class(course_row)
    would_bump = [
        r for r in all_results if r.is_passing and not r.qualifies_for_removal
    ]
    pass_mark = Setting.get_float("passing_percent", 60.0)

    return render_template(
        "gradebook/removal.html",
        course=course_row,
        item=item,
        candidates=candidates,
        scores=scores,
        would_bump=would_bump,
        pass_mark=pass_mark,
        scopes=((SCOPE_LECTURE, "Lecture"), (SCOPE_LABORATORY, "Laboratory")),
    )


# --------------------------------------------------------------------------- #
# Finalising
# --------------------------------------------------------------------------- #

@bp.route("/<int:course_id>/finalise", methods=["GET", "POST"])
@staff_required
def finalise(course_id: int):
    course_row = require_course(course_id)

    if request.method == "POST":
        if course_row.is_finalised:
            flash("This subject is already finalised.", "info")
            return redirect(url_for("gradebook.finalise", course_id=course_row.id))

        if request.form.get("confirm") != "FINALISE":
            flash("Type FINALISE to confirm.", "error")
            return redirect(url_for("gradebook.finalise", course_id=course_row.id))

        summary = finalise_course(course_row, current_user)
        log_action(current_user, "course_finalised", "course", course_row.id, str(summary))
        db.session.commit()

        parts = [f"{summary['passed']} passed", f"{summary['failed']} failed"]
        if summary.get("held_removal"):
            parts.append(f"{summary['removal_passed']} passed the removal")
            parts.append(f"{summary['removal_failed']} did not")
            parts.append(f"{summary['bumped']} adjusted one step higher")
        if summary["incomplete"]:
            parts.append(f"{summary['incomplete']} incomplete")

        flash("Grades finalised: " + ", ".join(parts) + ".", "success")
        return redirect(url_for("reports.gradesheet", course_id=course_row.id))

    results = compute_class(course_row)
    item = removal_item(course_row)
    held_removal = bool(
        item is not None
        and Score.query.filter(
            Score.item_id == item.id, Score.points.isnot(None)
        ).count()
    )

    # Preview exactly what finalising will do, before it is done.
    from ..grading import FAILING_GRADE, PASSING_GRADE, bump_one_step

    preview = []
    for result in results:
        after = result.grade
        note = ""
        if result.qualifies_for_removal:
            row = (
                Score.query.filter_by(item_id=item.id, student_id=result.student.id).first()
                if item is not None
                else None
            )
            if row is not None and row.points is not None and item.max_points:
                percent = row.points / item.max_points * 100
                if percent >= Setting.get_float("passing_percent", 60.0):
                    after, note = PASSING_GRADE, f"passed the removal ({percent:.1f}%)"
                else:
                    after, note = FAILING_GRADE, f"failed the removal ({percent:.1f}%)"
            else:
                after, note = FAILING_GRADE, "qualified but has no removal mark"
        elif held_removal and result.is_passing:
            after = bump_one_step(result.grade)
            if after != result.grade:
                note = "adjusted one step for the removal examination"
        preview.append({"result": result, "after": after, "note": note})

    return render_template(
        "gradebook/finalise.html",
        course=course_row,
        preview=preview,
        tally=class_tally(results),
        held_removal=held_removal,
        removal=item,
    )


@bp.route("/<int:course_id>/reopen", methods=["POST"])
@staff_required
def reopen(course_id: int):
    course_row = require_course(course_id)
    if not course_row.is_finalised:
        flash("This subject is not finalised.", "info")
        return redirect(url_for("gradebook.course", course_id=course_row.id))

    reopen_course(course_row)
    log_action(current_user, "course_reopened", "course", course_row.id)
    db.session.commit()
    flash(
        "The subject is open again. Grades are computed live once more, and "
        "any removal adjustment has been undone.",
        "success",
    )
    return redirect(url_for("gradebook.course", course_id=course_row.id))


# --------------------------------------------------------------------------- #
# One student
# --------------------------------------------------------------------------- #

@bp.route("/<int:course_id>/student/<int:student_id>")
@staff_required
def student_detail(course_id: int, student_id: int):
    course_row = require_course(course_id, manage=False)
    student = User.query.get_or_404(student_id)
    result = compute_student(course_row, student)
    return render_template(
        "gradebook/student.html", course=course_row, student=student, result=result
    )


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

@bp.route("/<int:course_id>/export.csv")
@staff_required
def export_csv(course_id: int):
    course_row = require_course(course_id, manage=False)
    section_id = request.args.get("section", type=int)
    results = compute_class(course_row, section_id)

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    header = ["Student number", "Name", "Section"]
    activities = []
    for component in sorted(course_row.components,
                            key=lambda c: (c.scope, c.order_index or 0)):
        for item_row in component.items:
            activities.append((component, item_row))
            header.append(
                f"{'LAB' if component.scope == SCOPE_LABORATORY else 'LEC'} "
                f"{component.name}: {item_row.title} /{item_row.max_points:g}"
            )
    header += ["Lecture %", "Laboratory %", "Final %", "Grade", "Remark"]
    writer.writerow(header)

    sections = {s.id: s.name for s in course_row.sections}
    from ..models import Enrollment

    enrolments = {
        e.student_id: sections.get(e.section_id, "")
        for e in Enrollment.query.filter_by(course_id=course_row.id).all()
    }

    scores = {
        (row.item_id, row.student_id): row
        for row in Score.query.join(
            GradeItem, Score.item_id == GradeItem.id
        )
        .join(GradeComponent, GradeItem.component_id == GradeComponent.id)
        .filter(GradeComponent.course_id == course_row.id)
        .all()
    }

    for result in results:
        row = [
            result.student.student_number or "",
            result.student.name,
            enrolments.get(result.student.id, ""),
        ]
        for _, item_row in activities:
            score_row = scores.get((item_row.id, result.student.id))
            row.append(
                f"{score_row.points:g}"
                if score_row is not None and score_row.points is not None
                else ""
            )
        row += [
            f"{result.lecture.percent:.2f}" if result.lecture.percent is not None else "",
            f"{result.laboratory.percent:.2f}"
            if result.laboratory and result.laboratory.percent is not None
            else "",
            result.final_display,
            result.effective_grade,
            result.effective_remark,
        ]
        writer.writerow(row)

    writer.writerow([])
    tally = class_tally(results)
    writer.writerow(["Passed", tally["passed"], "Removal", tally["removal"],
                     "Failed", tally["failed"], "Incomplete", tally["incomplete"]])

    import re

    name = re.sub(r"[^A-Za-z0-9]+", "-",
                  f"{course_row.code}-gradebook-{course_row.school_year}").strip("-")
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )
