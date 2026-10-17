"""Reading and writing questions as CSV.

One spreadsheet holds every kind of question this app supports. The columns a
row actually uses depend on its type, and anything irrelevant is left blank —
which is what makes a single sheet workable for a mixed paper.

    type        mcq | truefalse | numeric | short | matching | essay | multipart
    question    the question text; $...$ is rendered as mathematics
    points      marks for the question (per pair, for matching)
    answer      meaning depends on the type (see below)
    option_a..f the choices, or "left = right" pairs for matching
    tolerance   accepted error in per cent, for numerical answers
    unit        shown beside the answer box
    topic       free text, used to filter when drawing from the bank
    difficulty  easy | moderate | hard
    explanation shown to students after marking, if you release answers
    parts       the sub-items of a problem-solving question, separated by ;

``answer`` by type:

    mcq         the letter (B), the number (2), or the exact text of a choice
    truefalse   True or False
    numeric     the value, e.g. 202.65
    short       accepted answers separated by | , e.g. Boyle's law | Boyle
    matching    leave blank; the pairs carry their own answers
    essay       an optional guide answer, for the marker's eyes only
    multipart   leave blank; use the parts column

Every row is reported on. A row that cannot be read is named with its line
number and the reason, and the rest of the file still imports — a single typo
in row 40 should not cost you the other 39.
"""
from __future__ import annotations

import csv
import io

from .models import (
    Q_ESSAY,
    Q_MATCHING,
    Q_MCQ,
    Q_MULTIPART,
    Q_NUMERIC,
    Q_SHORT,
    Q_TRUEFALSE,
)

# --------------------------------------------------------------------------- #
# Shared vocabulary
# --------------------------------------------------------------------------- #

KNOWN_PART_TYPES = {
    "num": Q_NUMERIC, "numeric": Q_NUMERIC, "number": Q_NUMERIC,
    "short": Q_SHORT, "text": Q_SHORT, "word": Q_SHORT,
    "mcq": Q_MCQ, "choice": Q_MCQ,
    "tf": Q_TRUEFALSE, "truefalse": Q_TRUEFALSE,
    "essay": Q_ESSAY,
}

QUESTION_TYPE_WORDS = {
    "mcq": Q_MCQ, "multiple choice": Q_MCQ, "multiplechoice": Q_MCQ,
    "choice": Q_MCQ, "mc": Q_MCQ,
    "truefalse": Q_TRUEFALSE, "true or false": Q_TRUEFALSE,
    "true/false": Q_TRUEFALSE, "tf": Q_TRUEFALSE, "boolean": Q_TRUEFALSE,
    "numeric": Q_NUMERIC, "num": Q_NUMERIC, "number": Q_NUMERIC,
    "numerical": Q_NUMERIC, "computation": Q_NUMERIC,
    "short": Q_SHORT, "short answer": Q_SHORT, "identification": Q_SHORT,
    "matching": Q_MATCHING, "match": Q_MATCHING, "matching type": Q_MATCHING,
    "essay": Q_ESSAY, "long answer": Q_ESSAY, "discussion": Q_ESSAY,
    "multipart": Q_MULTIPART, "multi-part": Q_MULTIPART,
    "problem": Q_MULTIPART, "problem solving": Q_MULTIPART,
}

DIFFICULTIES = ("easy", "moderate", "hard")

HEADERS = [
    "type", "question", "points", "answer",
    "option_a", "option_b", "option_c", "option_d", "option_e", "option_f",
    "tolerance", "unit", "topic", "difficulty", "explanation", "parts",
]


def normalise_part_type(raw: str) -> str:
    return KNOWN_PART_TYPES.get((raw or "").strip().lower(), Q_NUMERIC)


def parse_parts_bulk(
    bulk: str, problems: list[str], semicolons: bool = False
) -> list[dict]:
    """Parse ``label | text | answer | …`` lines, order-tolerantly.

    After the first three fields, anything else is examined: a known type word
    becomes the answer type, and numbers fill points then tolerance. Assuming
    a fixed order here is what once made the editor crash on a perfectly
    reasonable line.

    In the editor one sub-item sits on each line. A spreadsheet cell cannot
    hold newlines reliably, so the CSV path passes ``semicolons=True`` and
    separates them with ``;`` instead.
    """
    out: list[dict] = []
    text = bulk or ""
    if semicolons:
        text = text.replace(";", "\n")
    lines = [ln for ln in text.splitlines() if ln.strip()]

    for line_no, line in enumerate(lines, start=1):
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 3:
            problems.append(
                f"sub-item {line_no}: needs at least ‘label | question | answer’"
            )
            continue

        label, text, answer = fields[0], fields[1], fields[2]
        if not answer:
            problems.append(f"sub-item {line_no}: the answer is blank")
            continue

        answer_type = Q_NUMERIC
        points: float | None = None
        tolerance: float | None = None
        unit = ""

        for extra in fields[3:]:
            if not extra:
                continue
            lowered = extra.lower()
            if lowered in KNOWN_PART_TYPES:
                answer_type = KNOWN_PART_TYPES[lowered]
                continue
            try:
                value = float(extra)
            except ValueError:
                if not unit:
                    unit = extra
                else:
                    problems.append(
                        f"sub-item {line_no}: could not understand “{extra}”"
                    )
                continue
            if points is None:
                points = value
            elif tolerance is None:
                tolerance = value
            else:
                problems.append(f"sub-item {line_no}: extra number “{extra}” ignored")

        out.append(
            {
                "label": label,
                "text": text,
                "answer": answer,
                "answer_type": answer_type,
                "points": points if points is not None else 1.0,
                "tolerance": tolerance if tolerance is not None else 0.0,
                "unit": unit,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _clean(value) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


def _float(value: str, default=None):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _normalise_header(name: str) -> str:
    """Accept the headings people actually type."""
    key = _clean(name).lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "question_type": "type", "kind": "type", "item_type": "type",
        "item": "question", "question_text": "question", "text": "question",
        "stem": "question", "problem": "question",
        "mark": "points", "marks": "points", "score": "points",
        "correct": "answer", "correct_answer": "answer", "key": "answer",
        "a": "option_a", "b": "option_b", "c": "option_c",
        "d": "option_d", "e": "option_e", "f": "option_f",
        "option1": "option_a", "option2": "option_b", "option3": "option_c",
        "option4": "option_d", "option5": "option_e", "option6": "option_f",
        "choice_a": "option_a", "choice_b": "option_b", "choice_c": "option_c",
        "choice_d": "option_d", "choice_e": "option_e", "choice_f": "option_f",
        "tolerance_percent": "tolerance", "tol": "tolerance",
        "units": "unit", "subject": "topic", "category": "topic",
        "level": "difficulty", "rationale": "explanation",
        "solution": "explanation", "sub_items": "parts", "subitems": "parts",
        "items": "parts",
    }
    return aliases.get(key, key)


def _options_from(row: dict) -> list[str]:
    out = []
    for letter in "abcdef":
        value = _clean(row.get(f"option_{letter}"))
        if value:
            out.append(value)
    return out


def parse_questions_csv(text: str) -> tuple[list[dict], list[str]]:
    """Read a question sheet into specifications, plus any problems found.

    Returns ``(specs, problems)``. A spec is a plain dict the caller turns
    into model objects, so this stays testable without a database.
    """
    problems: list[str] = []
    specs: list[dict] = []

    text = (text or "").lstrip("﻿")
    if not text.strip():
        return specs, ["The file is empty."]

    # Sniff the delimiter so a semicolon-separated export from Excel in a
    # European locale still works.
    sample = text[:4000]
    delimiter = ","
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        if sample.count(";") > sample.count(","):
            delimiter = ";"
        elif sample.count("\t") > sample.count(","):
            delimiter = "\t"

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return specs, ["The file has no header row."]

    columns = [_normalise_header(h) for h in header]
    if "question" not in columns:
        return specs, [
            "No ‘question’ column found. The first row must name the columns — "
            "download the template to see the headings."
        ]

    for line_no, raw in enumerate(reader, start=2):
        if not any(_clean(cell) for cell in raw):
            continue

        row = {columns[i]: raw[i] for i in range(min(len(columns), len(raw)))}

        question_text = _clean(row.get("question"))
        if not question_text:
            problems.append(f"row {line_no}: the question is blank")
            continue

        raw_type = _clean(row.get("type")).lower()
        qtype = QUESTION_TYPE_WORDS.get(raw_type)
        if qtype is None:
            if raw_type:
                problems.append(
                    f"row {line_no}: ‘{raw_type}’ is not a question type — "
                    f"using multiple choice"
                )
            qtype = Q_MCQ

        spec = {
            "qtype": qtype,
            "text": question_text,
            "points": _float(row.get("points"), 1.0) or 1.0,
            "topic": _clean(row.get("topic")),
            "explanation": _clean(row.get("explanation")),
            "unit": _clean(row.get("unit")),
            "tolerance_percent": _float(row.get("tolerance"), 0.0) or 0.0,
            "answer": _clean(row.get("answer")),
            "options": [],
            "parts": [],
        }

        difficulty = _clean(row.get("difficulty")).lower()
        spec["difficulty"] = difficulty if difficulty in DIFFICULTIES else "moderate"

        options = _options_from(row)

        # ---- per type ------------------------------------------------------ #
        if qtype == Q_MCQ:
            if len(options) < 2:
                problems.append(
                    f"row {line_no}: a multiple-choice question needs at least "
                    f"two options — skipped"
                )
                continue
            answer = spec["answer"]
            resolved = None
            if answer:
                if len(answer) == 1 and answer.upper() in "ABCDEF":
                    index = "ABCDEF".index(answer.upper())
                    if index < len(options):
                        resolved = options[index]
                elif answer.isdigit():
                    index = int(answer) - 1
                    if 0 <= index < len(options):
                        resolved = options[index]
                if resolved is None:
                    for option in options:
                        if option.lower() == answer.lower():
                            resolved = option
                            break
            if resolved is None:
                problems.append(
                    f"row {line_no}: the answer ‘{answer}’ does not match any "
                    f"option — skipped"
                )
                continue
            spec["options"] = options
            spec["answer"] = resolved

        elif qtype == Q_TRUEFALSE:
            answer = spec["answer"].lower()
            if answer in ("true", "t", "yes", "1"):
                spec["answer"] = "True"
            elif answer in ("false", "f", "no", "0"):
                spec["answer"] = "False"
            else:
                problems.append(
                    f"row {line_no}: the answer must be True or False — skipped"
                )
                continue
            spec["options"] = ["True", "False"]

        elif qtype == Q_NUMERIC:
            if _float(spec["answer"]) is None:
                problems.append(
                    f"row {line_no}: ‘{spec['answer']}’ is not a number — skipped"
                )
                continue

        elif qtype == Q_SHORT:
            if not spec["answer"]:
                problems.append(f"row {line_no}: no accepted answer given — skipped")
                continue

        elif qtype == Q_MATCHING:
            pairs = []
            source = options or [
                p for p in _clean(row.get("parts")).split(";") if p.strip()
            ]
            for entry in source:
                if "=" in entry:
                    left, right = entry.split("=", 1)
                elif "|" in entry:
                    left, right = entry.split("|", 1)
                else:
                    problems.append(
                        f"row {line_no}: ‘{entry}’ is not a pair — use ‘left = right’"
                    )
                    continue
                if left.strip() and right.strip():
                    pairs.append({"left": left.strip(), "right": right.strip()})
            if len(pairs) < 2:
                problems.append(
                    f"row {line_no}: a matching question needs at least two "
                    f"pairs — skipped"
                )
                continue
            for index, pair in enumerate(pairs):
                pair["answer"] = index
            spec["options"] = pairs
            spec["answer"] = ""

        elif qtype == Q_MULTIPART:
            row_problems: list[str] = []
            parts = parse_parts_bulk(
                _clean(row.get("parts")), row_problems, semicolons=True
            )
            for problem in row_problems:
                problems.append(f"row {line_no}: {problem}")
            if not parts:
                problems.append(
                    f"row {line_no}: a problem-solving question needs sub-items "
                    f"in the parts column — skipped"
                )
                continue
            spec["parts"] = parts
            spec["answer"] = ""

        specs.append(spec)

    if not specs and not problems:
        problems.append("No question rows were found below the header.")
    return specs, problems


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def questions_to_csv(questions) -> str:
    """Write questions back out in the same shape the importer reads."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADERS)

    for question in questions:
        options = ["", "", "", "", "", ""]
        answer = question.answer or ""
        parts = ""

        if question.qtype == Q_MATCHING:
            for index, pair in enumerate(question.pairs[:6]):
                options[index] = f"{pair.get('left', '')} = {pair.get('right', '')}"
            answer = ""
        elif question.qtype == Q_MULTIPART:
            parts = " ; ".join(
                f"{p.label} | {p.text} | {p.answer} | {p.answer_type} | "
                f"{p.points:g} | {(p.tolerance_percent or 0):g}"
                for p in question.parts
            )
            answer = ""
        else:
            for index, option in enumerate((question.options or [])[:6]):
                options[index] = option

        writer.writerow([
            question.qtype,
            question.text,
            f"{question.points:g}" if question.points is not None else "",
            answer,
            *options,
            f"{question.tolerance_percent:g}" if question.tolerance_percent else "",
            question.unit or "",
            question.topic or "",
            question.difficulty or "",
            question.explanation or "",
            parts,
        ])

    return buffer.getvalue()


def template_csv() -> str:
    """A filled-in example of every question type, to start from."""
    rows = [
        ["mcq",
         r"For an ideal gas, which is the equation of state?",
         "2", "A",
         "PV = mRT", "PV = nRT^{2}", "P/V = mRT", "PVT = mR", "", "",
         "", "", "Ideal gas", "easy",
         r"The ideal gas equation of state is $PV = mRT$.", ""],

        ["truefalse",
         "An isothermal process on an ideal gas has no change in internal energy.",
         "1", "True", "", "", "", "", "", "",
         "", "", "Processes", "easy", "", ""],

        ["numeric",
         r"Air at $101.325\ \text{kPa}$ is compressed isothermally to half its "
         r"volume. Find $P_{2}$.",
         "5", "202.65", "", "", "", "", "", "",
         "2", "kPa", "Ideal gas", "moderate",
         "Boyle's law: P1V1 = P2V2.", ""],

        ["short",
         "Name the law relating pressure and volume at constant temperature.",
         "2", "Boyle's law | Boyles law | Boyle", "", "", "", "", "", "",
         "", "", "Gas laws", "easy", "", ""],

        ["matching",
         "Match each process with the property that stays constant.",
         "2", "",
         "Isothermal = Temperature", "Isobaric = Pressure",
         "Isochoric = Volume", "Adiabatic = No heat transfer", "", "",
         "", "", "Processes", "moderate", "", ""],

        ["essay",
         "Explain why a heat engine cannot convert all its heat into work.",
         "10", "", "", "", "", "", "", "",
         "", "", "Second law", "hard", "", ""],

        ["multipart",
         r"A rigid tank of $0.5\ \text{m}^{3}$ holds air at $200\ \text{kPa}$ "
         r"and $27^{\circ}\text{C}$. Take $R = 0.287$.",
         "1", "", "", "", "", "", "", "",
         "", "", "Ideal gas", "hard", "",
         "a | Find the mass of air | 1.162 | num | 4 | 2 ; "
         "b | Find the specific volume | 0.4304 | num | 3 | 2 ; "
         "c | Name the process if heated | isochoric | short | 3"],
    ]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return buffer.getvalue()
