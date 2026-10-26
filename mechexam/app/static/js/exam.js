/* Exam runtime.
   -------------------------------------------------------------------------
   Responsibilities, in order of how much trouble they caused:

   1. ONE CLOCK FOR THE CLASS.  The remaining time comes from the server (the
      exam carries closes_at, set when the instructor opened it).  The client
      counts down locally between polls and re-syncs regularly, so a student
      cannot gain time by reloading, and the exam closes for everybody at the
      same moment -- including when nobody is watching the monitor page,
      because the first client to notice expiry tells the server.

   2. NOTHING IS LOST.  Answers are posted as they are typed (debounced), on
      every change, before the page is hidden, and on submit.

   3. LEAVING IS NOT THE SAME AS THE SCREEN SLEEPING.  A phone whose backlight
      times out and a student switching to another app produce the SAME
      events: the document hides and the window loses focus.  An earlier
      version of this file claimed focus told them apart.  On a desktop it
      does; on a phone it does not -- locking an Android or iOS handset blurs
      the window exactly as an app switch would -- so that check quietly
      failed on the devices the students actually use, and a locked phone was
      submitted as though the student had walked away.

      Since the two cannot be distinguished at the moment they happen, this
      version does not try to.  It waits.  Nothing is submitted while the
      student is away.  Coming back quickly is an interruption -- a screen
      that timed out, an incoming call, a notification pulled down -- and is
      recorded without penalty.  Only an absence longer than the lock grace,
      by which time a locked phone would have been picked up and unlocked,
      counts as leaving, and even then the exam forgives the first few.
      Retained focus is still used, but only one way: if the window kept
      focus, it was certainly a sleeping screen.
*/
(function () {
  "use strict";

  var root = document.querySelector("[data-exam-runtime]");
  if (!root) return;

  var config = {
    attemptId: root.getAttribute("data-attempt-id"),
    autosaveUrl: root.getAttribute("data-autosave-url"),
    eventUrl: root.getAttribute("data-event-url"),
    statusUrl: root.getAttribute("data-status-url"),
    expireUrl: root.getAttribute("data-expire-url"),
    submitUrl: root.getAttribute("data-submit-url"),
    exitMode: root.getAttribute("data-exit-mode") || "off",
    graceSeconds: parseInt(root.getAttribute("data-grace-seconds") || "180", 10),
    lockGraceSeconds: parseInt(root.getAttribute("data-lock-grace-seconds") || "90", 10),
    exitAllowance: parseInt(root.getAttribute("data-exit-allowance") || "1", 10),
    idleSeconds: parseInt(root.getAttribute("data-idle-seconds") || "0", 10),
    autosaveSeconds: parseInt(root.getAttribute("data-autosave-seconds") || "20", 10),
    clockPollSeconds: parseInt(root.getAttribute("data-clock-poll-seconds") || "60", 10),
    blockPaste: root.getAttribute("data-block-paste") === "1",
    secondsRemaining: parseInt(root.getAttribute("data-seconds-remaining") || "0", 10)
  };

  var form = document.querySelector("[data-exam-form]");
  var timerNode = document.querySelector("[data-timer]");
  var timerClock = timerNode ? timerNode.querySelector(".clock") : null;
  var saveState = document.querySelector("[data-save-state]");
  var progressBar = document.querySelector("[data-exam-progress] span");
  var progressText = document.querySelector("[data-answered-count]");
  var overlay = document.querySelector("[data-exam-overlay]");

  var remaining = config.secondsRemaining;
  var finished = false;
  var saveTimer = null;
  var pendingSave = false;
  var graceTimer = null;
  var graceLeft = 0;
  var hiddenAt = 0;          // when the document was last hidden
  var absences = 0;          // confirmed departures so far
  var touchedSincePoll = false;   // any real input since the last status poll
  var lastTouchAt = Date.now();   // when the student last did anything
  var awayTimer = null;           // fires if an absence outlasts the allowance

  /* ---- helpers ---------------------------------------------------------- */

  function post(url, data, onDone) {
    var request = new XMLHttpRequest();
    request.open("POST", url, true);
    request.setRequestHeader("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8");
    request.onload = function () {
      var payload = null;
      try { payload = JSON.parse(request.responseText); } catch (e) { payload = null; }
      if (onDone) onDone(request.status, payload);
    };
    request.onerror = function () { if (onDone) onDone(0, null); };
    request.send(data);
    return request;
  }

  function serialise() {
    if (!form) return "";
    var parts = [];
    var fields = form.querySelectorAll("input, textarea, select");

    for (var i = 0; i < fields.length; i++) {
      var field = fields[i];
      if (!field.name) continue;
      if (field.type === "submit" || field.type === "button") continue;
      if ((field.type === "radio" || field.type === "checkbox") && !field.checked) continue;
      parts.push(encodeURIComponent(field.name) + "=" + encodeURIComponent(field.value));
    }
    return parts.join("&");
  }

  function setSaveState(state, text) {
    if (!saveState) return;
    saveState.className = "save-state " + (state || "");
    var label = saveState.querySelector(".label");
    if (label) label.textContent = text;
  }

  function formatTime(seconds) {
    if (seconds < 0) seconds = 0;
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var secs = seconds % 60;
    function pad(n) { return n < 10 ? "0" + n : String(n); }
    if (hours > 0) return hours + ":" + pad(minutes) + ":" + pad(secs);
    return minutes + ":" + pad(secs);
  }

  /* ---- saving ----------------------------------------------------------- */

  function saveNow(onDone) {
    if (finished || !config.autosaveUrl) { if (onDone) onDone(); return; }
    pendingSave = false;
    lastSaveAt = Date.now();
    setSaveState("saving", "Saving…");

    post(config.autosaveUrl, serialise(), function (status, payload) {
      if (status === 200 && payload && payload.ok) {
        setSaveState("", "Saved " + (payload.saved_at || ""));
        if (typeof payload.seconds_remaining === "number") {
          remaining = payload.seconds_remaining;
        }
      } else if (status === 409 && payload && payload.redirect) {
        finished = true;
        window.location.href = payload.redirect;
        return;
      } else {
        setSaveState("failed", "Not saved — will retry");
      }
      if (onDone) onDone();
    });
  }

  /* How long after the typing stops before we save. Short on purpose: this
     is what makes a dead battery cost a student nothing. */
  var QUIET_MS = 1200;

  /* The administrator's "save answers every N seconds" is a THROTTLE, not a
     timer: answers are never more than N seconds stale, and we never send
     more than one save per N seconds per student. That second half is what
     keeps a sixty-student exam affordable. */
  var minGapMs = Math.max(5, config.autosaveSeconds) * 1000;
  var lastSaveAt = 0;

  function scheduleSave() {
    if (finished) return;
    pendingSave = true;
    setSaveState("saving", "Saving…");
    if (saveTimer) clearTimeout(saveTimer);

    var sinceLast = Date.now() - lastSaveAt;
    var wait = Math.max(QUIET_MS, minGapMs - sinceLast);
    saveTimer = setTimeout(saveNow, wait);
  }

  /* A beacon survives the page being torn down, unlike XHR. */
  /* A farewell save, sent as the page goes away. It is marked as such: a
     beacon is asynchronous and arrives a moment AFTER the event reporting
     that the exam left the screen, so without this flag the save would land
     second and tell the server the student was back -- which cancelled the
     very absence it was sent during. */
  function saveWithBeacon(leaving) {
    if (finished || !config.autosaveUrl) return;
    try {
      if (navigator.sendBeacon) {
        var body = serialise() + (leaving ? "&leaving=1" : "");
        var blob = new Blob([body], {
          type: "application/x-www-form-urlencoded;charset=UTF-8"
        });
        navigator.sendBeacon(config.autosaveUrl, blob);
      }
    } catch (e) { /* best effort */ }
  }

  /* ---- progress --------------------------------------------------------- */

  function updateProgress() {
    if (!form) return;
    var cards = form.querySelectorAll("[data-question-card]");
    var answered = 0;

    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      var fields = card.querySelectorAll("input, textarea, select");
      var done = false;

      for (var f = 0; f < fields.length; f++) {
        var field = fields[f];
        if (!field.name) continue;
        if (field.type === "radio" || field.type === "checkbox") {
          if (field.checked) { done = true; break; }
        } else if (field.value && String(field.value).trim() !== "") {
          done = true; break;
        }
      }

      if (done) answered++;
      var index = card.getAttribute("data-question-index");
      var dot = document.querySelector('[data-nav-index="' + index + '"]');
      if (dot) {
        if (done) dot.classList.add("answered");
        else dot.classList.remove("answered");
      }
    }

    if (progressText) progressText.textContent = String(answered);
    if (progressBar && cards.length) {
      progressBar.style.width = Math.round(answered / cards.length * 100) + "%";
    }
  }

  /* ---- the clock -------------------------------------------------------- */

  function tick() {
    if (finished) return;
    remaining -= 1;

    if (timerClock) timerClock.textContent = formatTime(remaining);
    if (timerNode) {
      timerNode.classList.remove("warn", "danger");
      if (remaining <= 60) timerNode.classList.add("danger");
      else if (remaining <= 300) timerNode.classList.add("warn");
    }

    if (remaining === 300 || remaining === 60) {
      announce(remaining === 60
        ? "One minute left."
        : "Five minutes left.");
    }

    if (remaining <= 0) {
      timeUp();
    }
  }

  function timeUp() {
    if (finished) return;
    finished = true;
    if (timerClock) timerClock.textContent = "0:00";

    showOverlay(
      "Time is up",
      "Your answers are being submitted now. Please wait.",
      null
    );

    // Tell the server the exam has expired, so it closes for everyone --
    // then submit this attempt.
    if (config.expireUrl) {
      post(config.expireUrl, "", function () { doSubmit("time_expired"); });
    } else {
      doSubmit("time_expired");
    }
  }

  /* Re-sync with the server; the server's figure always wins. */
  function syncStatus() {
    if (finished || !config.statusUrl) return;
    var request = new XMLHttpRequest();
    // Tell the server whether this student has actually done anything since
    // the last poll, which is what an inactivity limit is measured against.
    var url = config.statusUrl +
              (config.statusUrl.indexOf("?") === -1 ? "?" : "&") +
              "active=" + (touchedSincePoll ? "1" : "0");
    touchedSincePoll = false;
    request.open("GET", url, true);
    request.onload = function () {
      if (request.status !== 200) return;
      var payload;
      try { payload = JSON.parse(request.responseText); } catch (e) { return; }

      if (payload.submitted && payload.redirect) {
        finished = true;
        window.location.href = payload.redirect;
        return;
      }
      if (typeof payload.seconds_remaining === "number") {
        remaining = payload.seconds_remaining;
        if (timerClock) timerClock.textContent = formatTime(remaining);
      }
      if (!payload.open && !payload.submitted) {
        // The instructor closed the exam while we were working.
        finished = true;
        showOverlay("The exam has been closed",
                    "Your answers are being submitted.", null);
        doSubmit("closed_by_instructor");
      }
    };
    request.send();
  }

  /* ---- submitting ------------------------------------------------------- */

  function doSubmit(reason) {
    if (!form) return;
    var auto = form.querySelector('[name="auto"]');
    var why = form.querySelector('[name="reason"]');
    if (auto) auto.value = "1";
    if (why) why.value = reason || "auto";
    window.removeEventListener("beforeunload", warnOnLeave);
    form.submit();
  }

  function manualSubmit() {
    var cards = form ? form.querySelectorAll("[data-question-card]").length : 0;
    var answered = progressText ? parseInt(progressText.textContent || "0", 10) : 0;
    var message = "Submit your answers now?";

    if (cards && answered < cards) {
      message = "You have answered " + answered + " of " + cards +
                " questions. Unanswered questions score zero. Submit anyway?";
    }
    if (!window.confirm(message)) return;

    finished = true;
    window.removeEventListener("beforeunload", warnOnLeave);
    if (form) form.submit();
  }

  /* ---- overlay ---------------------------------------------------------- */

  function showOverlay(title, body, countdown) {
    if (!overlay) return;
    var heading = overlay.querySelector("[data-overlay-title]");
    var text = overlay.querySelector("[data-overlay-body]");
    var counter = overlay.querySelector("[data-overlay-countdown]");

    if (heading) heading.textContent = title;
    if (text) text.textContent = body;
    if (counter) {
      counter.style.display = countdown === null ? "none" : "";
      if (countdown !== null) counter.textContent = String(countdown);
    }
    overlay.classList.add("shown");
  }

  function hideOverlay() {
    if (overlay) overlay.classList.remove("shown");
  }

  function announce(message) {
    var live = document.querySelector("[data-exam-announce]");
    if (live) live.textContent = message;
  }

  /* ---- integrity -------------------------------------------------------- */

  function report(kind, detail, onDone) {
    if (!config.eventUrl) { if (onDone) onDone(0, null); return; }
    var body = "kind=" + encodeURIComponent(kind) +
               "&detail=" + encodeURIComponent(detail || "") +
               "&" + serialise();
    post(config.eventUrl, body, onDone);
  }

  function noteActivity() {
    touchedSincePoll = true;
    lastTouchAt = Date.now();
  }

  /* The inactivity limit, when one is set. The server applies it too, from
     what the status poll reports, so a frozen page cannot sit it out. */
  function checkIdle() {
    if (finished || !config.idleSeconds) return;
    var idle = Math.round((Date.now() - lastTouchAt) / 1000);
    if (idle < config.idleSeconds) return;
    finished = true;
    showOverlay(
      "Your exam has been submitted",
      "There was no activity for " + idle + " seconds, which ends the exam.",
      null
    );
    doSubmit("idle");
  }

  /* Did the window keep focus while hidden?  If so it was certainly the
     screen going dark, because nothing came in front of the exam.  The
     converse does NOT hold: a locked phone also loses focus, so a false here
     proves nothing on its own. */
  function keptFocus() {
    try {
      return document.hasFocus();
    } catch (e) {
      return false;
    }
  }

  /* Hiding saves the work and tells the server, which from that moment keeps
     its own clock on the absence. This page may stop running entirely -- a
     locked phone fires no timers -- so the server, not this timer, is what
     actually ends the exam. The timer below is only there to end it promptly
     on a device that does keep running. */
  function onHidden() {
    if (finished) return;

    saveWithBeacon(true);
    hiddenAt = Date.now();

    report(
      keptFocus() ? "screen_sleep" : "hidden",
      "exam left the screen — the server is timing this absence"
    );

    if (config.exitMode === "off") return;

    if (awayTimer) clearTimeout(awayTimer);
    awayTimer = setTimeout(function () {
      if (finished || !document.hidden) return;
      // Still away past the allowance. Submit from here if this device is
      // still alive; if it is not, the server has already done it.
      finished = true;
      saveWithBeacon(true);
      report("exit_confirmed", "still away past the allowance");
      doSubmit("exit_away");
    }, Math.max(5, config.lockGraceSeconds) * 1000);
  }

  /* Coming back is where the decision is actually made, because by then the
     length of the absence is known. */
  function onVisible() {
    if (finished) return;

    if (awayTimer) { clearTimeout(awayTimer); awayTimer = null; }

    var awaySeconds = hiddenAt ? Math.round((Date.now() - hiddenAt) / 1000) : 0;
    hiddenAt = 0;
    noteActivity();

    if (graceTimer) {
      clearInterval(graceTimer);
      graceTimer = null;
      hideOverlay();
    }

    if (awaySeconds <= 0) { syncStatus(); return; }

    if (awaySeconds < Math.max(5, config.lockGraceSeconds)) {
      // Short enough to be a screen lock, a call or a notification. A phone
      // that was put down and picked up is not a student who walked out.
      report(
        "interruption",
        "away " + awaySeconds + "s — within the allowance for a locked screen"
      );
      syncStatus();
      return;
    }

    absences += 1;
    if (absences <= Math.max(0, config.exitAllowance)) {
      report(
        "absence_forgiven",
        "away " + awaySeconds + "s — " + absences + " of " +
        config.exitAllowance + " forgiven"
      );
      showOverlay(
        "You were away from the exam",
        "You were away for " + awaySeconds + " seconds. That has been noted " +
        "for your instructor. Leaving again may end your exam.",
        null
      );
      setTimeout(hideOverlay, 6000);
      syncStatus();
      return;
    }

    confirmedExit(awaySeconds);
  }

  /* Only reached once an absence is too long to be a lock and the allowance
     is used up.  The exam is submitted now, on return -- never while the
     student is away and unable to see what happened. */
  function confirmedExit(awaySeconds) {
    if (config.exitMode === "off") {
      report("exit", "away " + awaySeconds + "s — recorded only");
      syncStatus();
      return;
    }

    if (config.exitMode === "grace") {
      report("exit", "away " + awaySeconds + "s — grace period started");
      startGrace();
      return;
    }

    report(
      "exit_confirmed",
      "away " + awaySeconds + "s after " + absences + " absences",
      function (status, payload) {
        if (payload && payload.action === "submitted" && payload.redirect) {
          finished = true;
          showOverlay(
            "Your exam has been submitted",
            "You were away from the exam for " + awaySeconds + " seconds. " +
            "Leaving submits the exam, as announced before you began.",
            null
          );
          setTimeout(function () {
            window.location.href = payload.redirect;
          }, 4000);
        }
      }
    );
  }

  function startGrace() {
    graceLeft = config.graceSeconds;
    showOverlay(
      "Return to the exam",
      "You left the exam. It will be submitted if you do not come back in time.",
      graceLeft
    );

    if (graceTimer) clearInterval(graceTimer);
    graceTimer = setInterval(function () {
      graceLeft -= 1;
      var counter = overlay ? overlay.querySelector("[data-overlay-countdown]") : null;
      if (counter) counter.textContent = String(graceLeft);

      if (graceLeft <= 0) {
        clearInterval(graceTimer);
        graceTimer = null;
        finished = true;
        showOverlay("Submitting", "The grace period has run out.", null);
        doSubmit("exit_grace_expired");
      }
    }, 1000);
  }

  function warnOnLeave(e) {
    if (finished) return undefined;
    e.preventDefault();
    e.returnValue = "";
    return "";
  }

  /* ---- paging ----------------------------------------------------------- */

  /* "One question at a time" and "allow going back" only mean something if the
     paper is actually paged, so the paging is done here rather than leaving
     two settings that quietly do nothing. With scripting unavailable the whole
     paper is simply visible, which is the safe way to fail. */
  function initPaging() {
    if (!form) return;
    var onePerPage = root.getAttribute("data-one-per-page") === "1";
    if (!onePerPage) return;

    var cards = form.querySelectorAll("[data-question-card]");
    if (cards.length < 2) return;

    var allowBack = root.getAttribute("data-allow-backtrack") === "1";
    var current = 0;
    var furthest = 0;

    var bar = document.createElement("div");
    bar.className = "flex-between card-body";
    bar.style.borderTop = "1px solid var(--line)";
    bar.innerHTML =
      '<button type="button" class="button" data-page-prev>Previous</button>' +
      '<span class="small muted" data-page-label></span>' +
      '<button type="button" class="button button-primary" data-page-next>Next</button>';

    var host = document.createElement("div");
    host.className = "card";
    host.appendChild(bar);
    cards[cards.length - 1].parentNode.insertBefore(
      host, cards[cards.length - 1].nextSibling
    );

    var prev = bar.querySelector("[data-page-prev]");
    var next = bar.querySelector("[data-page-next]");
    var label = bar.querySelector("[data-page-label]");

    function show(index) {
      if (index < 0 || index >= cards.length) return;
      if (!allowBack && index < current) return;

      current = index;
      if (index > furthest) furthest = index;

      for (var i = 0; i < cards.length; i++) {
        cards[i].style.display = i === index ? "" : "none";
      }

      label.textContent = "Question " + (index + 1) + " of " + cards.length;
      prev.style.visibility = (allowBack && index > 0) ? "visible" : "hidden";
      next.textContent = index === cards.length - 1 ? "Last question" : "Next";
      next.disabled = index === cards.length - 1;

      try {
        window.scrollTo(0, 0);
      } catch (e) { /* ignore */ }
    }

    prev.addEventListener("click", function () {
      saveNow(function () { show(current - 1); });
    });
    next.addEventListener("click", function () {
      saveNow(function () { show(current + 1); });
    });

    // The question strip jumps straight to a question when that is allowed.
    var dots = document.querySelectorAll("[data-nav-index]");
    for (var d = 0; d < dots.length; d++) {
      (function (dot) {
        dot.addEventListener("click", function (e) {
          e.preventDefault();
          var target = parseInt(dot.getAttribute("data-nav-index"), 10) - 1;
          if (!allowBack && target < current) return;
          saveNow(function () { show(target); });
        });
      })(dots[d]);
    }

    show(0);
  }

  /* ---- calculator ------------------------------------------------------- */

  function initCalculator() {
    var panel = document.querySelector("[data-calculator]");
    var toggle = document.querySelector("[data-calculator-toggle]");
    if (!panel || !toggle) return;

    var screen = panel.querySelector("[data-calc-screen]");
    var buffer = "";

    function show() { screen.textContent = buffer || "0"; }

    toggle.addEventListener("click", function () {
      panel.classList.toggle("shown");
    });

    var keys = panel.querySelectorAll("[data-calc-key]");
    for (var i = 0; i < keys.length; i++) {
      keys[i].addEventListener("click", function (e) {
        var key = e.currentTarget.getAttribute("data-calc-key");

        if (key === "C") { buffer = ""; show(); return; }
        if (key === "DEL") { buffer = buffer.slice(0, -1); show(); return; }
        if (key === "=") {
          try {
            // Only arithmetic characters ever reach the evaluator.
            if (!/^[0-9+\-*/().eE%^ ]*$/.test(buffer)) throw new Error("bad");
            var expression = buffer.replace(/\^/g, "**").replace(/%/g, "/100");
            /* jshint evil:true */
            var value = Function('"use strict";return (' + expression + ")")();
            buffer = (value === undefined || value === null || isNaN(value))
              ? "" : String(Math.round(value * 1e10) / 1e10);
          } catch (err) {
            buffer = "";
            screen.textContent = "Error";
            return;
          }
          show();
          return;
        }
        buffer += key;
        show();
      });
    }

    var closer = panel.querySelector("[data-calc-close]");
    if (closer) {
      closer.addEventListener("click", function () { panel.classList.remove("shown"); });
    }
    show();
  }

  function initScratchpad() {
    var panel = document.querySelector("[data-scratchpad]");
    var toggle = document.querySelector("[data-scratchpad-toggle]");
    if (!panel || !toggle) return;

    var area = panel.querySelector("textarea");
    var key = "mechexam-scratch-" + config.attemptId;

    // Per-device convenience only; never read back by the server.
    try {
      var saved = sessionStorage.getItem(key);
      if (saved && area) area.value = saved;
    } catch (e) { /* private mode */ }

    if (area) {
      area.addEventListener("input", function () {
        try { sessionStorage.setItem(key, area.value); } catch (e) { /* ignore */ }
      });
    }

    toggle.addEventListener("click", function () { panel.classList.toggle("shown"); });
    var closer = panel.querySelector("[data-scratch-close]");
    if (closer) {
      closer.addEventListener("click", function () { panel.classList.remove("shown"); });
    }
  }

  /* ---- wiring ----------------------------------------------------------- */

  function init() {
    document.body.setAttribute("data-exam-mode", "1");

    if (timerClock) timerClock.textContent = formatTime(remaining);
    setSaveState("", "Saved");
    updateProgress();

    if (form) {
      form.addEventListener("input", function () {
        updateProgress();
        scheduleSave();
      });
      form.addEventListener("change", function () {
        updateProgress();
        scheduleSave();
      });
      form.addEventListener("submit", function () {
        finished = true;
        window.removeEventListener("beforeunload", warnOnLeave);
      });
    }

    var submitButton = document.querySelector("[data-submit-exam]");
    if (submitButton) {
      submitButton.addEventListener("click", function (e) {
        e.preventDefault();
        saveNow(manualSubmit);
      });
    }

    setInterval(tick, 1000);

    // The countdown is kept locally, so this poll only re-syncs the clock and
    // notices an exam the instructor has closed early. Polling it every few
    // seconds costs real money on a metered host and buys nothing.
    setInterval(syncStatus, Math.max(15, config.clockPollSeconds) * 1000);

    setInterval(function () {
      if (pendingSave) saveNow();
    }, Math.max(5, config.autosaveSeconds) * 1000);

    var activityEvents = ["keydown", "pointerdown", "touchstart", "wheel",
                          "scroll", "input", "change"];
    for (var a = 0; a < activityEvents.length; a++) {
      document.addEventListener(activityEvents[a], noteActivity,
                                { passive: true, capture: true });
    }
    if (config.idleSeconds) setInterval(checkIdle, 2000);

    document.addEventListener("visibilitychange", function () {
      if (document.hidden) onHidden();
      else onVisible();
    });

    window.addEventListener("blur", function () {
      // Never acted on. A phone locking, a notification, a second window and
      // a student switching apps all blur the exam, so a blur on its own says
      // nothing. It is recorded for the instructor's monitor and no more; the
      // decision belongs to onVisible, which knows how long the absence was.
      if (document.hidden) return;
      if (config.exitMode !== "off") report("blur", "window lost focus (not acted on)");
    });

    window.addEventListener("pagehide", function () { saveWithBeacon(true); });
    window.addEventListener("beforeunload", warnOnLeave);

    if (config.blockPaste) {
      document.addEventListener("paste", function (e) {
        var target = e.target;
        if (target && form && form.contains(target)) {
          e.preventDefault();
          report("paste", "paste blocked");
          announce("Pasting is not allowed in this exam.");
        }
      });
      document.addEventListener("contextmenu", function (e) {
        if (form && form.contains(e.target)) e.preventDefault();
      });
    }

    initPaging();
    initCalculator();
    initScratchpad();

    if (window.MechExam && window.MechExam.renderMath) {
      window.MechExam.renderMath(document.body);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
