/* Shared behaviour: sidebar, clock, theme, table filters, confirmations.
   Written as plain ES5-compatible script so it runs on the older Android
   browsers students actually have. */
(function () {
  "use strict";

  /* ---- sidebar ---------------------------------------------------------- */

  function initSidebar() {
    var toggle = document.querySelector("[data-sidebar-toggle]");
    var scrim = document.querySelector(".sidebar-scrim");
    if (!toggle) return;

    function close() { document.body.classList.remove("sidebar-open"); }

    toggle.addEventListener("click", function () {
      document.body.classList.toggle("sidebar-open");
    });
    if (scrim) scrim.addEventListener("click", close);

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
    });

    // Closing on navigation keeps the drawer from lingering over the new page.
    var links = document.querySelectorAll(".sidebar a");
    for (var i = 0; i < links.length; i++) {
      links[i].addEventListener("click", close);
    }
  }

  /* ---- live clock ------------------------------------------------------- */

  function initClock() {
    var targets = document.querySelectorAll("[data-clock]");
    if (!targets.length) return;

    function pad(n) { return n < 10 ? "0" + n : String(n); }

    var days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday"];
    var months = ["January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November", "December"];

    function tick() {
      var now = new Date();
      var hours = now.getHours();
      var suffix = hours >= 12 ? "PM" : "AM";
      var shown = hours % 12;
      if (shown === 0) shown = 12;

      var time = shown + ":" + pad(now.getMinutes()) + ":" + pad(now.getSeconds()) + " " + suffix;
      var date = days[now.getDay()] + ", " + now.getDate() + " " +
                 months[now.getMonth()] + " " + now.getFullYear();

      for (var i = 0; i < targets.length; i++) {
        var node = targets[i];
        var timeNode = node.querySelector("[data-clock-time]");
        var dateNode = node.querySelector("[data-clock-date]");
        if (timeNode) timeNode.textContent = time;
        if (dateNode) dateNode.textContent = date;
        if (!timeNode && !dateNode) node.textContent = time;
      }
    }

    tick();
    setInterval(tick, 1000);
  }

  /* ---- theme ------------------------------------------------------------ */

  function initTheme() {
    var stored = null;
    try { stored = localStorage.getItem("mechexam-theme"); } catch (e) { stored = null; }

    var declared = document.documentElement.getAttribute("data-theme");
    if (!declared && stored) document.documentElement.setAttribute("data-theme", stored);

    var buttons = document.querySelectorAll("[data-theme-set]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", function (e) {
        var mode = e.currentTarget.getAttribute("data-theme-set");
        if (mode === "system") {
          document.documentElement.removeAttribute("data-theme");
        } else {
          document.documentElement.setAttribute("data-theme", mode);
        }
        try { localStorage.setItem("mechexam-theme", mode); } catch (err) { /* private mode */ }
      });
    }
  }

  /* ---- confirmations ---------------------------------------------------- */

  function initConfirm() {
    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (!form || !form.getAttribute) return;
      var message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) {
        e.preventDefault();
        return false;
      }
    }, true);

    var links = document.querySelectorAll("a[data-confirm]");
    for (var i = 0; i < links.length; i++) {
      links[i].addEventListener("click", function (e) {
        if (!window.confirm(e.currentTarget.getAttribute("data-confirm"))) {
          e.preventDefault();
        }
      });
    }
  }

  /* ---- client-side table filter ----------------------------------------- */

  function initFilters() {
    var inputs = document.querySelectorAll("[data-filter-target]");

    for (var i = 0; i < inputs.length; i++) {
      (function (input) {
        var selector = input.getAttribute("data-filter-target");
        var counter = input.getAttribute("data-filter-count");

        function apply() {
          var needle = input.value.toLowerCase().trim();
          var rows = document.querySelectorAll(selector);
          var shown = 0;

          for (var r = 0; r < rows.length; r++) {
            var row = rows[r];
            var haystack = (row.getAttribute("data-search") || row.textContent || "").toLowerCase();
            var match = !needle || haystack.indexOf(needle) !== -1;
            row.style.display = match ? "" : "none";
            if (match) shown++;
          }

          if (counter) {
            var node = document.querySelector(counter);
            if (node) node.textContent = String(shown);
          }
        }

        input.addEventListener("input", apply);
        apply();
      })(inputs[i]);
    }
  }

  /* ---- tick-all checkboxes ---------------------------------------------- */

  function initCheckAll() {
    var masters = document.querySelectorAll("[data-check-all]");

    for (var i = 0; i < masters.length; i++) {
      (function (master) {
        var selector = master.getAttribute("data-check-all");

        master.addEventListener("change", function () {
          var boxes = document.querySelectorAll(selector);
          for (var b = 0; b < boxes.length; b++) {
            // Only touch rows a filter has left visible, so "tick all" after
            // narrowing to one section does not quietly take in everybody.
            var row = boxes[b].closest("tr, .check-row");
            if (row && row.style.display === "none") continue;
            boxes[b].checked = master.checked;
          }
          updateCount(selector);
        });

        var boxes = document.querySelectorAll(selector);
        for (var b = 0; b < boxes.length; b++) {
          boxes[b].addEventListener("change", function () { updateCount(selector); });
        }
      })(masters[i]);
    }

    function updateCount(selector) {
      var boxes = document.querySelectorAll(selector);
      var n = 0;
      for (var b = 0; b < boxes.length; b++) if (boxes[b].checked) n++;
      var out = document.querySelectorAll("[data-checked-count]");
      for (var o = 0; o < out.length; o++) out[o].textContent = String(n);
    }
  }

  /* ---- live totals (gradebook, rubrics) --------------------------------- */

  function initLiveTotals() {
    var groups = document.querySelectorAll("[data-total-group]");

    for (var i = 0; i < groups.length; i++) {
      (function (group) {
        var inputs = group.querySelectorAll("[data-total-input]");
        var output = group.querySelector("[data-total-output]");
        var maxNode = group.querySelector("[data-total-max]");
        var percentNode = group.querySelector("[data-total-percent]");
        if (!output) return;

        function recompute() {
          var sum = 0;
          var max = 0;
          // Radio buttons come in groups sharing a name: only the checked one
          // counts towards the total, and the group's cap counts once.
          var seenGroups = {};

          for (var n = 0; n < inputs.length; n++) {
            var input = inputs[n];
            var isRadio = input.type === "radio";
            var group = input.name || "field-" + n;

            if (isRadio) {
              if (input.checked) {
                var picked = parseFloat(input.value);
                if (!isNaN(picked)) sum += picked;
              }
            } else {
              var typed = parseFloat(input.value);
              if (!isNaN(typed)) sum += typed;
            }

            if (!seenGroups[group]) {
              seenGroups[group] = true;
              var cap = parseFloat(input.getAttribute("max") || "0");
              if (!isNaN(cap)) max += cap;
            }
          }
          output.textContent = (Math.round(sum * 100) / 100).toString();
          if (maxNode) maxNode.textContent = (Math.round(max * 100) / 100).toString();
          if (percentNode) {
            percentNode.textContent = max > 0
              ? (Math.round(sum / max * 10000) / 100).toFixed(2) + "%"
              : "—";
          }
        }

        for (var n = 0; n < inputs.length; n++) {
          inputs[n].addEventListener("input", recompute);
          inputs[n].addEventListener("change", recompute);
        }
        recompute();
      })(groups[i]);
    }
  }

  /* ---- rubric scale buttons --------------------------------------------- */

  function initRubricScales() {
    var scales = document.querySelectorAll(".rubric-scale");

    for (var i = 0; i < scales.length; i++) {
      (function (scale) {
        var labels = scale.querySelectorAll("label");

        function paint() {
          for (var l = 0; l < labels.length; l++) {
            var input = labels[l].querySelector("input");
            if (input && input.checked) labels[l].classList.add("picked");
            else labels[l].classList.remove("picked");
          }
        }

        for (var l = 0; l < labels.length; l++) {
          var input = labels[l].querySelector("input");
          if (input) input.addEventListener("change", paint);
        }
        paint();
      })(scales[i]);
    }
  }

  /* ---- dependent selects (subject -> sections / activities) -------------- */

  function initDependentSelects() {
    var sources = document.querySelectorAll("[data-load-into]");

    for (var i = 0; i < sources.length; i++) {
      (function (source) {
        var targetSelector = source.getAttribute("data-load-into");
        var urlTemplate = source.getAttribute("data-load-url");
        var placeholder = source.getAttribute("data-load-placeholder") || "— none —";
        if (!targetSelector || !urlTemplate) return;

        source.addEventListener("change", function () {
          var target = document.querySelector(targetSelector);
          if (!target) return;
          var value = source.value;
          if (!value) {
            target.innerHTML = '<option value="">' + placeholder + "</option>";
            return;
          }

          var request = new XMLHttpRequest();
          request.open("GET", urlTemplate.replace("__ID__", value), true);
          request.onload = function () {
            if (request.status !== 200) return;
            var rows;
            try { rows = JSON.parse(request.responseText); } catch (e) { return; }
            var html = '<option value="">' + placeholder + "</option>";
            for (var r = 0; r < rows.length; r++) {
              var label = rows[r].label || rows[r].name || String(rows[r].id);
              html += '<option value="' + rows[r].id + '">' +
                      label.replace(/</g, "&lt;") + "</option>";
            }
            target.innerHTML = html;
          };
          request.send();
        });
      })(sources[i]);
    }
  }

  /* ---- unsaved-changes guard -------------------------------------------- */

  function initDirtyGuard() {
    var forms = document.querySelectorAll("form[data-warn-unsaved]");
    if (!forms.length) return;
    var dirty = false;

    for (var i = 0; i < forms.length; i++) {
      forms[i].addEventListener("input", function () { dirty = true; });
      forms[i].addEventListener("submit", function () { dirty = false; });
    }

    window.addEventListener("beforeunload", function (e) {
      if (!dirty) return undefined;
      e.preventDefault();
      e.returnValue = "";
      return "";
    });
  }

  /* ---- question type groups --------------------------------------------- */

  /* The question editor carries the fields for every question type at once, so
     switching type never discards what has been typed. Several of those groups
     contain a field called "answer", so the groups that are not in use must be
     DISABLED as well as hidden -- a hidden-but-enabled input still submits,
     and the server would read the wrong one. */
  function initQuestionTypes() {
    var editors = document.querySelectorAll(".question-editor");

    for (var i = 0; i < editors.length; i++) {
      (function (editor) {
        var select = editor.querySelector(".qtype-select");
        if (!select) return;

        var groups = editor.querySelectorAll(".type-group");
        var notes = editor.querySelectorAll(".type-note");

        function apply() {
          var current = select.value;

          for (var g = 0; g < groups.length; g++) {
            var group = groups[g];
            var types = (group.getAttribute("data-types") || "").split(/\s+/);
            var active = types.indexOf(current) !== -1;

            group.style.display = active ? "" : "none";

            var fields = group.querySelectorAll("input, textarea, select");
            for (var f = 0; f < fields.length; f++) {
              fields[f].disabled = !active;
            }
          }

          for (var n = 0; n < notes.length; n++) {
            notes[n].style.display =
              notes[n].getAttribute("data-for") === current ? "" : "none";
          }
        }

        select.addEventListener("change", apply);
        apply();
      })(editors[i]);
    }
  }

  /* ---- auto-dismiss flashes --------------------------------------------- */

  function initFlashes() {
    var flashes = document.querySelectorAll(".callout-success");
    for (var i = 0; i < flashes.length; i++) {
      (function (node) {
        setTimeout(function () {
          node.style.transition = "opacity 0.4s";
          node.style.opacity = "0";
          setTimeout(function () {
            if (node.parentNode) node.parentNode.removeChild(node);
          }, 420);
        }, 9000);
      })(flashes[i]);
    }
  }

  /* ---- service worker --------------------------------------------------- */

  function initServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    if (location.protocol !== "https:" && location.hostname !== "localhost" &&
        location.hostname !== "127.0.0.1") return;
    // Never cache anything while an exam is on screen.
    if (document.body.getAttribute("data-exam-mode") === "1") return;

    navigator.serviceWorker.register("/service-worker.js", { scope: "/" })
      .catch(function () { /* offline support is a bonus, not a requirement */ });
  }

  /* ---- install prompt --------------------------------------------------- */

  function initInstallPrompt() {
    var button = document.querySelector("[data-install]");
    if (!button) return;
    var deferred = null;

    window.addEventListener("beforeinstallprompt", function (e) {
      e.preventDefault();
      deferred = e;
      button.style.display = "inline-flex";
    });

    button.addEventListener("click", function () {
      if (!deferred) {
        window.alert(
          "To install: open your browser menu and choose “Add to Home screen”."
        );
        return;
      }
      deferred.prompt();
      deferred = null;
      button.style.display = "none";
    });
  }

  /* ---- boot ------------------------------------------------------------- */

  function boot() {
    initSidebar();
    initClock();
    initTheme();
    initConfirm();
    initFilters();
    initCheckAll();
    initLiveTotals();
    initRubricScales();
    initDependentSelects();
    initQuestionTypes();
    initDirtyGuard();
    initFlashes();
    initServiceWorker();
    initInstallPrompt();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.MechExam = window.MechExam || {};
})();
