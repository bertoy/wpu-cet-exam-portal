"""The authoring assistant: a conversation that ends in reviewed questions.

The design worth noting is what this blueprint does *not* do. It never creates
a Question. When the assistant produces a draft paper, the CSV it wrote is
handed to the ordinary bank importer, which parses it with
``question_io.parse_questions_csv`` and shows the same review screen as a
spreadsheet uploaded by hand. So the assistant has exactly the authority of a
person typing into Excel, and no more: every question is seen before it is
saved, and a malformed answer is reported row by row rather than stored.

Conversations are kept so an instructor can come back to one, and belong to
the instructor who held them.
"""
from __future__ import annotations

import re

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

from .. import ai
from ..extensions import db
from ..models import (
    AIConversation,
    AIMessage,
    QuestionBank,
    log_action,
    utcnow,
)
from ..security import staff_required, visible_courses

bp = Blueprint("assistant", __name__)

# How much of the conversation is sent back as context. Enough to follow a
# thread of revisions, short enough not to resend a whole afternoon's work.
CONTEXT_TURNS = 12

STARTERS = [
    ("Ten numerical problems on the first law of thermodynamics for a "
     "two-hour examination, moderate difficulty, with tolerances."),
    ("A twenty-item multiple-choice quiz on free and forced vibration of a "
     "single-degree-of-freedom system."),
    ("One long problem-solving question on a rigid tank of air, with four "
     "lettered sub-items that build on each other."),
    ("Five matching-type items pairing thermodynamic processes with the "
     "property that stays constant."),
    ("Rewrite these three questions so they test understanding rather than "
     "recall, and tell me what changed."),
]


def _conversation(conversation_id: int) -> AIConversation:
    conversation = AIConversation.query.get_or_404(conversation_id)
    if conversation.user_id != current_user.id and not current_user.is_superadmin:
        abort(404)
    return conversation


def _history(conversation: AIConversation) -> list[dict]:
    recent = conversation.messages[-CONTEXT_TURNS:]
    return [{"role": m.role, "content": m.content} for m in recent if m.content]


CSV_BLOCK = re.compile(r"```(?:csv)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def sheet_rows(sheet: str) -> int:
    """How many questions a sheet holds: its non-empty lines, less the header."""
    lines = [ln for ln in (sheet or "").splitlines() if ln.strip()]
    return max(0, len(lines) - 1)


def strip_csv(text: str) -> str:
    """The reply with the sheet taken out.

    The CSV is for the importer to read, not for a person: sixteen columns
    wrapped in a chat bubble are unreadable, and the review screen shows the
    questions properly a moment later. What the assistant said *about* the
    questions is worth reading, so that stays.
    """
    replaced = CSV_BLOCK.sub("", text or "").strip()
    return replaced or "(a question sheet — see below)"


def extract_csv(text: str) -> str:
    """The first fenced block that looks like a question sheet.

    Kept as its own function so it can be tested without the API: what the
    model wraps its answer in is exactly the sort of thing that changes.
    """
    for block in CSV_BLOCK.findall(text or ""):
        body = block.strip()
        first = body.splitlines()[0].lower() if body.splitlines() else ""
        if "question" in first and "," in first:
            return body
    # No fence: accept a bare sheet if the text itself starts like one.
    stripped = (text or "").strip()
    head = stripped.splitlines()[0].lower() if stripped.splitlines() else ""
    if head.startswith("type,") and "question" in head:
        return stripped
    return ""


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

@bp.route("/")
@staff_required
def index():
    conversations = (
        AIConversation.query.filter_by(user_id=current_user.id)
        .order_by(AIConversation.updated_at.desc())
        .limit(30)
        .all()
    )
    return render_template(
        "assistant/index.html",
        conversations=conversations,
        starters=STARTERS,
        configured=ai.is_configured(),
        enabled=ai.is_enabled(),
        model=ai.current_model(),
    )


@bp.route("/new", methods=["POST"])
@staff_required
def new():
    conversation = AIConversation(
        user_id=current_user.id,
        title="New conversation",
        course_id=request.form.get("course_id", type=int) or None,
    )
    db.session.add(conversation)
    db.session.commit()
    return redirect(
        url_for("assistant.chat", conversation_id=conversation.id,
                prompt=request.form.get("prompt") or None)
    )


@bp.route("/<int:conversation_id>")
@staff_required
def chat(conversation_id: int):
    conversation = _conversation(conversation_id)
    course_ids = [c.id for c in visible_courses().all()]
    banks = (
        QuestionBank.query.filter(QuestionBank.course_id.in_(course_ids or [-1]))
        .order_by(QuestionBank.name)
        .all()
    )

    # Any sheet the assistant has produced, newest first, so the instructor
    # can send an earlier draft to a bank after asking for a revision.
    drafts = []
    for message in conversation.messages:
        if message.role != "assistant":
            continue
        sheet = extract_csv(message.content)
        if sheet:
            drafts.append({"message": message, "count": sheet_rows(sheet)})

    return render_template(
        "assistant/chat.html",
        conversation=conversation,
        banks=banks,
        courses=visible_courses().all(),
        drafts=list(reversed(drafts)),
        strip_csv=strip_csv,
        extract_csv=extract_csv,
        sheet_rows=sheet_rows,
        starters=STARTERS,
        prompt=request.args.get("prompt", ""),
        configured=ai.is_configured(),
        enabled=ai.is_enabled(),
        model=ai.current_model(),
    )


@bp.route("/<int:conversation_id>/say", methods=["POST"])
@staff_required
def say(conversation_id: int):
    conversation = _conversation(conversation_id)
    text = (request.form.get("message") or "").strip()
    if not text:
        flash("Type something to ask.", "error")
        return redirect(url_for("assistant.chat", conversation_id=conversation.id))

    if not ai.is_enabled():
        flash(
            "The assistant is switched off or has no API key. An administrator "
            "sets one up in Administration ▸ Assistant.",
            "error",
        )
        return redirect(url_for("assistant.chat", conversation_id=conversation.id))

    db.session.add(AIMessage(conversation_id=conversation.id, role="user",
                             content=text[:20000]))
    if conversation.title in ("", "New conversation", None):
        conversation.title = text[:70] + ("…" if len(text) > 70 else "")
    conversation.updated_at = utcnow()
    db.session.commit()

    context = ""
    if conversation.course:
        course = conversation.course
        context = (
            f"\n\nThe lecturer is working on {course.code} — {course.title}"
            f" ({course.term_label})."
        )

    try:
        reply = ai.chat(_history(conversation), system=ai.AUTHORING_SYSTEM + context)
    except ai.AIError as error:
        # Recorded, not merely flashed: when an instructor reports that "it
        # stopped working", the conversation itself should say what happened.
        db.session.add(AIMessage(
            conversation_id=conversation.id, role="error", content=str(error)
        ))
        db.session.commit()
        flash(str(error), "error")
        return redirect(url_for("assistant.chat", conversation_id=conversation.id))

    db.session.add(AIMessage(conversation_id=conversation.id, role="assistant",
                             content=reply))
    conversation.updated_at = utcnow()
    db.session.commit()
    return redirect(
        url_for("assistant.chat", conversation_id=conversation.id) + "#latest"
    )


@bp.route("/message/<int:message_id>/to-bank", methods=["POST"])
@staff_required
def to_bank(message_id: int):
    """Send a drafted sheet to a bank — by way of the ordinary import review.

    Nothing is saved here. The CSV is handed to the importer, which parses it
    exactly as it would a spreadsheet and shows the instructor every question
    before any of it is written.
    """
    message = AIMessage.query.get_or_404(message_id)
    conversation = _conversation(message.conversation_id)

    bank = QuestionBank.query.get_or_404(request.form.get("bank_id", type=int))
    if bank.course_id not in [c.id for c in visible_courses().all()]:
        abort(404)

    sheet = extract_csv(message.content)
    if not sheet:
        flash(
            "That reply does not contain a question sheet. Ask the assistant "
            "for the questions as CSV.",
            "error",
        )
        return redirect(url_for("assistant.chat", conversation_id=conversation.id))

    log_action(
        current_user, "assistant.draft", "question_bank", bank.id,
        f"draft from conversation {conversation.id} sent for review",
    )
    db.session.commit()

    # Straight into the existing review screen, which is the only path that
    # can create questions.
    return render_template(
        "assistant/handoff.html",
        bank=bank,
        sheet=sheet,
        conversation=conversation,
    )


@bp.route("/<int:conversation_id>/delete", methods=["POST"])
@staff_required
def delete(conversation_id: int):
    conversation = _conversation(conversation_id)
    db.session.delete(conversation)
    db.session.commit()
    flash("Conversation deleted.", "success")
    return redirect(url_for("assistant.index"))
