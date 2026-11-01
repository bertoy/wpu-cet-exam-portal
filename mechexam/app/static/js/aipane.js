/*  The assistant pane.
 *
 *  A side panel that knows what page it was opened on and can draft, check
 *  or explain against it. It renders replies as plain text with a copy
 *  button beside each one, because the pane is deliberately unable to write
 *  anything itself: copying is how a suggestion becomes a question, a mark
 *  or a remark, and that step is the lecturer's.
 *
 *  Notes for anyone editing this:
 *
 *  * Replies are inserted with textContent, never innerHTML. The text comes
 *    back from a language model, which is not a trusted author, and a side
 *    panel that renders arbitrary markup on every page of the portal is
 *    exactly the sort of thing that turns into a cross-site scripting hole.
 *  * The thread is kept in memory only. Close the pane and it is gone. The
 *    pane is for a question and an answer, not a record.
 *  * Everything degrades: no key, no internet, no permission -- the pane
 *    says so in the panel rather than failing silently.
 */
(function () {
  "use strict";

  var pane = document.querySelector("[data-ai-pane]");
  var launch = document.querySelector("[data-ai-open]");
  if (!pane || !launch) { return; }

  var thread = pane.querySelector("[data-ai-thread]");
  var intro = pane.querySelector("[data-ai-intro]");
  var actionBox = pane.querySelector("[data-ai-actions]");
  var noteBox = pane.querySelector("[data-ai-note]");
  var looking = pane.querySelector("[data-ai-looking]");
  var form = pane.querySelector("[data-ai-form]");
  var input = pane.querySelector("[data-ai-input]");
  var send = pane.querySelector("[data-ai-send]");

  var kind = pane.getAttribute("data-ai-kind") || "";
  var ident = pane.getAttribute("data-ai-id") || "";
  var history = [];
  var busy = false;
  var started = false;
  var ready = true;          // until /ai/state says otherwise

  function note(message, tone) {
    if (!message) { noteBox.hidden = true; return; }
    noteBox.hidden = false;
    noteBox.textContent = message;
    noteBox.className = "ai-pane-note" + (tone ? " ai-pane-note-" + tone : "");
  }

  function scroll() { thread.scrollTop = thread.scrollHeight; }

  function bubble(role, text) {
    var wrap = document.createElement("div");
    wrap.className = "ai-msg ai-msg-" + role;

    var body = document.createElement("div");
    body.className = "ai-msg-body";
    body.textContent = text;                 // never innerHTML -- see above
    wrap.appendChild(body);

    if (role === "assistant") {
      var tools = document.createElement("div");
      tools.className = "ai-msg-tools";

      var copy = document.createElement("button");
      copy.type = "button";
      copy.className = "ai-chip";
      copy.textContent = "Copy";
      copy.addEventListener("click", function () {
        var done = function () {
          copy.textContent = "Copied";
          setTimeout(function () { copy.textContent = "Copy"; }, 1600);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, function () {
            fallbackCopy(text); done();
          });
        } else { fallbackCopy(text); done(); }
      });
      tools.appendChild(copy);
      wrap.appendChild(tools);
    }

    thread.appendChild(wrap);
    scroll();
    return wrap;
  }

  function fallbackCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); } catch (error) { /* nothing to do */ }
    document.body.removeChild(area);
  }

  function ask(question) {
    if (busy || !question || !ready) { return; }
    busy = true;
    send.disabled = true;
    if (intro) { intro.hidden = true; }
    actionBox.hidden = true;

    bubble("user", question);
    var waiting = bubble("assistant", "Thinking…");
    waiting.classList.add("is-waiting");

    fetch("/ai/ask", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        question: question, kind: kind, id: ident,
        history: history.slice(-8)
      })
    })
      .then(function (response) {
        if (response.status === 401 || response.status === 403) {
          throw new Error("You are not signed in as staff any more. "
                          + "Reload the page.");
        }
        return response.json();
      })
      .then(function (data) {
        thread.removeChild(waiting);
        if (!data.ok) { note(data.error || "That did not work.", "warn"); return; }
        note("");
        history.push({ role: "user", text: question });
        history.push({ role: "assistant", text: data.text });
        bubble("assistant", data.text);
        if (window.renderMathInElement) {
          try {
            window.renderMathInElement(thread, {
              delimiters: [
                { left: "$$", right: "$$", display: true },
                { left: "$", right: "$", display: false }
              ],
              throwOnError: false
            });
          } catch (error) { /* plain text is a fine fallback */ }
        }
      })
      .catch(function (error) {
        if (waiting.parentNode) { thread.removeChild(waiting); }
        note(error.message || "Could not reach the assistant.", "warn");
      })
      .then(function () {
        busy = false;
        send.disabled = false;
        input.focus();
        scroll();
      });
  }

  function loadActions() {
    fetch("/ai/actions?kind=" + encodeURIComponent(kind)
          + "&id=" + encodeURIComponent(ident))
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.looking_at) {
          looking.textContent = "· looking at " + data.looking_at;
        }
        actionBox.innerHTML = "";
        (data.actions || []).forEach(function (action) {
          var button = document.createElement("button");
          button.type = "button";
          button.className = "ai-action";
          button.disabled = !ready;
          button.textContent = action.label;
          button.addEventListener("click", function () { ask(action.prompt); });
          actionBox.appendChild(button);
        });
      })
      .catch(function () { /* the pane still works without quick actions */ });
  }

  /* The pane cannot work. Say why once, and stop offering things that would
     only fail: an action button that cannot run is worse than no button. */
  function stand_down(reason) {
    ready = false;
    note(reason, "warn");
    input.disabled = true;
    send.disabled = true;
    input.placeholder = "Not available on this copy of the portal";
    var buttons = actionBox.querySelectorAll(".ai-action");
    for (var i = 0; i < buttons.length; i += 1) { buttons[i].disabled = true; }
  }

  function checkReady() {
    fetch("/ai/state")
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (!data.ready) { stand_down(data.reason); }
      })
      .catch(function () {
        stand_down("The assistant cannot be reached from this laptop. If an "
                   + "examination is running on the room's own network, that "
                   + "is expected — there is no internet.");
      });
  }

  function open() {
    pane.hidden = false;
    document.body.classList.add("ai-pane-open");
    launch.setAttribute("aria-expanded", "true");
    if (!started) { started = true; checkReady(); loadActions(); }
    setTimeout(function () { input.focus(); }, 60);
  }

  function close() {
    pane.hidden = true;
    document.body.classList.remove("ai-pane-open");
    launch.setAttribute("aria-expanded", "false");
    launch.focus();
  }

  launch.addEventListener("click", function () {
    if (pane.hidden) { open(); } else { close(); }
  });
  pane.querySelector("[data-ai-close]").addEventListener("click", close);

  document.addEventListener("keydown", function (event) {
    if (event.altKey && (event.key === "a" || event.key === "A")) {
      event.preventDefault();
      if (pane.hidden) { open(); } else { close(); }
    } else if (event.key === "Escape" && !pane.hidden) {
      close();
    }
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var question = input.value.trim();
    if (!question) { return; }
    input.value = "";
    ask(question);
  });

  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });
})();
