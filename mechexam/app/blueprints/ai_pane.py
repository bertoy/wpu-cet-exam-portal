"""The assistant pane: an add-in that opens beside the page you are on.

Like the pane in Word, this slides in over the right-hand side of whatever
you are doing and can see it. It drafts, checks and explains. It does not
save.

**That last sentence is enforced by what this file contains, not by a flag.**
There is no model import here that could create a row, no ``db.session.add``,
no ``commit``. The only thing this blueprint can do is read context, ask the
model, and hand the text back to the browser for the lecturer to copy. If a
future change wants the pane to write something, it will have to add the
machinery to do it, which is a visible act rather than a quiet one --- and
``test_rules.py`` fails if that machinery appears.

Staff only, by decorator. A student never gets the pane: not on their own
pages, and above all not while sitting an examination.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from .. import ai, ai_context
from ..security import staff_required

bp = Blueprint("aipane", __name__)

# A pane is for a question and an answer, not an afternoon's conversation.
# Enough turns to refine a draft, few enough to stay quick and cheap.
MAX_TURNS = 8
MAX_QUESTION = 4000


@bp.route("/state")
@staff_required
def state():
    """Whether the pane can work at all, in words the lecturer can act on."""
    if not ai.is_enabled():
        return jsonify({
            "ready": False,
            "reason": "The assistant is switched off. An administrator turns "
                      "it on in Administration ▸ Assistant.",
        })
    if not ai.is_configured():
        return jsonify({
            "ready": False,
            "reason": "No API key is set. An administrator sets one in "
                      "Administration ▸ Assistant.",
        })
    return jsonify({"ready": True, "model": ai.current_model()})


@bp.route("/actions")
@staff_required
def actions():
    """The quick actions for the kind of page the pane was opened on."""
    kind = request.args.get("kind", "")
    label, _ = ai_context.gather(kind, request.args.get("id"))
    return jsonify({
        "actions": ai_context.actions_for(kind if label else ""),
        "looking_at": label,
    })


@bp.route("/ask", methods=["POST"])
@staff_required
def ask():
    """One turn. Returns text; writes nothing, anywhere."""
    if not ai.is_enabled() or not ai.is_configured():
        return jsonify({"ok": False,
                        "error": "The assistant is not set up on this copy of "
                                 "the portal."}), 200

    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()[:MAX_QUESTION]
    if not question:
        return jsonify({"ok": False, "error": "Ask something first."}), 200

    label, context = ai_context.gather(payload.get("kind", ""),
                                       payload.get("id"))

    system = ai_context.PANE_SYSTEM
    if context:
        system += (f"\n\nThe lecturer is looking at {label}. Here it is:\n\n"
                   f"{context}\n")

    messages = []
    for turn in (payload.get("history") or [])[-MAX_TURNS:]:
        role = turn.get("role")
        text = (turn.get("text") or "").strip()
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": text[:MAX_QUESTION]})
    messages.append({"role": "user", "content": question})

    try:
        reply = ai.chat(messages, system=system, max_tokens=3000)
    except ai.AIError as error:
        return jsonify({"ok": False, "error": str(error)}), 200
    except Exception as error:                              # noqa: BLE001
        # Most often: no internet, because the laptop is running the
        # examination on a network with no way out. Say that, rather than
        # showing a stack trace in a side panel.
        return jsonify({
            "ok": False,
            "error": f"Could not reach the assistant ({error}). If this "
                     f"laptop is running an examination on the room's own "
                     f"network, there is no internet, which is the point — "
                     f"the pane will work again once you are back online.",
        }), 200

    return jsonify({"ok": True, "text": reply, "looking_at": label})
