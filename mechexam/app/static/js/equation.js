/* Equation editor.
   -------------------------------------------------------------------------
   A MathType-like helper over plain LaTeX, rendered by the *locally bundled*
   KaTeX -- no CDN, so it works on a campus connection that cannot reach the
   open internet, and nothing breaks if a CDN changes.

   Any field marked data-equation gets a toolbar; the dialog offers a symbol
   palette grouped into Basic, Calculus, Greek and Engineering, with a live
   preview.  Maths is written between $ ... $ and rendered wherever the page
   calls MechExam.renderMath(). */
(function () {
  "use strict";

  var PALETTES = {
    basic: [
      ["x^{2}", "x²"], ["x_{n}", "xₙ"], ["\\frac{a}{b}", "a/b"],
      ["\\sqrt{x}", "√x"], ["\\sqrt[3]{x}", "∛x"], ["\\pm", "±"],
      ["\\times", "×"], ["\\div", "÷"], ["\\cdot", "·"],
      ["\\leq", "≤"], ["\\geq", "≥"], ["\\neq", "≠"],
      ["\\approx", "≈"], ["\\propto", "∝"], ["\\infty", "∞"],
      ["\\left( \\right)", "( )"], ["\\left[ \\right]", "[ ]"],
      ["\\left\\{ \\right\\}", "{ }"], ["\\left| \\right|", "| |"],
      ["\\%", "%"], ["^{\\circ}", "°"], ["\\angle", "∠"],
      ["\\overline{AB}", "AB̄"], ["\\vec{F}", "F⃗"],
      ["\\begin{matrix} a & b \\\\ c & d \\end{matrix}", "matrix"]
    ],
    calculus: [
      ["\\frac{dy}{dx}", "dy/dx"], ["\\frac{\\partial u}{\\partial x}", "∂u/∂x"],
      ["\\frac{d^{2}y}{dx^{2}}", "d²y/dx²"],
      ["\\int", "∫"], ["\\int_{a}^{b}", "∫ᵃᵇ"], ["\\iint", "∬"], ["\\oint", "∮"],
      ["\\sum_{i=1}^{n}", "Σ"], ["\\prod_{i=1}^{n}", "Π"],
      ["\\lim_{x \\to 0}", "lim"], ["\\nabla", "∇"],
      ["\\sin", "sin"], ["\\cos", "cos"], ["\\tan", "tan"],
      ["\\ln", "ln"], ["\\log", "log"], ["e^{x}", "eˣ"],
      ["\\sinh", "sinh"], ["\\cosh", "cosh"],
      ["\\to", "→"], ["\\Rightarrow", "⇒"], ["\\therefore", "∴"],
      ["\\Delta", "Δ"], ["\\partial", "∂"], ["dx", "dx"]
    ],
    greek: [
      ["\\alpha", "α"], ["\\beta", "β"], ["\\gamma", "γ"], ["\\delta", "δ"],
      ["\\epsilon", "ε"], ["\\zeta", "ζ"], ["\\eta", "η"], ["\\theta", "θ"],
      ["\\kappa", "κ"], ["\\lambda", "λ"], ["\\mu", "μ"], ["\\nu", "ν"],
      ["\\xi", "ξ"], ["\\pi", "π"], ["\\rho", "ρ"], ["\\sigma", "σ"],
      ["\\tau", "τ"], ["\\upsilon", "υ"], ["\\phi", "φ"], ["\\chi", "χ"],
      ["\\psi", "ψ"], ["\\omega", "ω"],
      ["\\Gamma", "Γ"], ["\\Delta", "Δ"], ["\\Theta", "Θ"], ["\\Lambda", "Λ"],
      ["\\Pi", "Π"], ["\\Sigma", "Σ"], ["\\Phi", "Φ"], ["\\Omega", "Ω"]
    ],
    engineering: [
      ["\\text{kPa}", "kPa"], ["\\text{MPa}", "MPa"], ["\\text{kN}", "kN"],
      ["\\text{kJ/kg}", "kJ/kg"], ["\\text{kJ/kg} \\cdot \\text{K}", "kJ/kg·K"],
      ["\\text{kW}", "kW"], ["\\text{kg/s}", "kg/s"], ["\\text{m}^{3}/\\text{kg}", "m³/kg"],
      ["\\text{m/s}^{2}", "m/s²"], ["^{\\circ}\\text{C}", "°C"], ["\\text{K}", "K"],
      ["\\text{N} \\cdot \\text{m}", "N·m"], ["\\text{rev/min}", "rev/min"],
      ["\\eta_{th}", "η_th"], ["\\eta_{v}", "η_v"], ["c_{p}", "cₚ"], ["c_{v}", "c_v"],
      ["\\dot{m}", "ṁ"], ["\\dot{Q}", "Q̇"], ["\\dot{W}", "Ẇ"],
      ["\\rho", "ρ"], ["\\mu", "μ"], ["\\nu", "ν"],
      ["P_{1}V_{1} = P_{2}V_{2}", "P₁V₁=P₂V₂"],
      ["\\frac{P}{\\rho} + \\frac{v^{2}}{2} + gz", "Bernoulli"],
      ["Re = \\frac{\\rho v D}{\\mu}", "Re"],
      ["\\sigma = \\frac{F}{A}", "σ=F/A"],
      ["\\tau = \\frac{T r}{J}", "τ=Tr/J"]
    ]
  };

  var dialog = null;
  var activeField = null;

  /* ---- rendering -------------------------------------------------------- */

  function katexReady() {
    return typeof window.katex !== "undefined";
  }

  /* Render $...$ spans inside a container. */
  function renderMath(root) {
    root = root || document.body;
    if (!katexReady()) return;

    var nodes = root.querySelectorAll("[data-math]");
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.getAttribute("data-math-done") === "1") continue;
      renderInto(node, node.getAttribute("data-math-source") || node.textContent || "");
      node.setAttribute("data-math-done", "1");
    }
  }

  /* Split text on $...$ and $$...$$, rendering the maths and escaping the rest. */
  function renderInto(node, source) {
    if (!katexReady()) {
      node.textContent = source;
      return;
    }

    var html = "";
    var index = 0;
    var pattern = /\$\$([\s\S]+?)\$\$|\$([^\$\n]+?)\$/g;
    var match;

    while ((match = pattern.exec(source)) !== null) {
      html += escapeHtml(source.slice(index, match.index));
      var display = match[1] !== undefined;
      var latex = display ? match[1] : match[2];
      try {
        html += window.katex.renderToString(latex, {
          displayMode: display,
          throwOnError: false,
          strict: false,
          output: "html"
        });
      } catch (e) {
        html += '<code class="eqn-bad">' + escapeHtml(match[0]) + "</code>";
      }
      index = pattern.lastIndex;
    }
    html += escapeHtml(source.slice(index));
    node.innerHTML = html.replace(/\n/g, "<br>");
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  /* ---- toolbars --------------------------------------------------------- */

  var QUICK = [
    ["$x^{2}$", "x²"],
    ["$\\frac{a}{b}$", "a/b"],
    ["$\\sqrt{x}$", "√"],
    ["$\\pi$", "π"],
    ["$\\Delta$", "Δ"],
    ["$^{\\circ}$", "°"],
    ["$\\times$", "×"],
    ["$\\leq$", "≤"]
  ];

  function attachToolbars() {
    var fields = document.querySelectorAll("[data-equation]");

    for (var i = 0; i < fields.length; i++) {
      (function (field) {
        if (field.getAttribute("data-equation-ready") === "1") return;
        field.setAttribute("data-equation-ready", "1");

        var bar = document.createElement("div");
        bar.className = "eqn-toolbar";

        var open = document.createElement("button");
        open.type = "button";
        open.className = "button button-small";
        open.innerHTML = "&#8730; Equation";
        open.title = "Insert an equation (opens the symbol palette)";
        open.addEventListener("click", function () { openDialog(field); });
        bar.appendChild(open);

        for (var q = 0; q < QUICK.length; q++) {
          (function (pair) {
            var chip = document.createElement("button");
            chip.type = "button";
            chip.className = "eqn-chip";
            chip.textContent = pair[1];
            chip.title = pair[0];
            chip.addEventListener("click", function () { insert(field, pair[0]); });
            bar.appendChild(chip);
          })(QUICK[q]);
        }

        var peek = document.createElement("button");
        peek.type = "button";
        peek.className = "button button-small button-quiet";
        peek.textContent = "Preview";
        bar.appendChild(peek);

        var preview = document.createElement("div");
        preview.className = "eqn-preview";
        preview.style.display = "none";
        preview.style.marginTop = "0.4rem";

        field.parentNode.insertBefore(bar, field);
        if (field.nextSibling) {
          field.parentNode.insertBefore(preview, field.nextSibling);
        } else {
          field.parentNode.appendChild(preview);
        }

        peek.addEventListener("click", function () {
          if (preview.style.display === "none") {
            preview.style.display = "";
            renderInto(preview, field.value);
            peek.textContent = "Hide preview";
          } else {
            preview.style.display = "none";
            peek.textContent = "Preview";
          }
        });

        field.addEventListener("input", function () {
          if (preview.style.display !== "none") renderInto(preview, field.value);
        });
      })(fields[i]);
    }
  }

  function insert(field, text) {
    var start = field.selectionStart;
    var end = field.selectionEnd;

    if (typeof start !== "number") {
      field.value += text;
    } else {
      field.value = field.value.slice(0, start) + text + field.value.slice(end);
      var caret = start + text.length;
      // Park the caret inside the first empty {} if there is one, which is
      // what makes the palette feel like an editor rather than a stamp.
      var hole = text.indexOf("{}");
      if (hole !== -1) caret = start + hole + 1;
      field.setSelectionRange(caret, caret);
    }
    field.focus();

    var event;
    try {
      event = new Event("input", { bubbles: true });
    } catch (e) {
      event = document.createEvent("Event");
      event.initEvent("input", true, true);
    }
    field.dispatchEvent(event);
  }

  /* ---- dialog ----------------------------------------------------------- */

  function buildDialog() {
    if (dialog) return dialog;

    var modal = document.createElement("div");
    modal.className = "eqn-modal";
    modal.innerHTML =
      '<div class="eqn-dialog" role="dialog" aria-modal="true" aria-label="Equation editor">' +
        '<header><h3>Equation editor</h3>' +
          '<button type="button" class="button button-small" data-eqn-close>Close</button>' +
        "</header>" +
        '<div class="body">' +
          '<div class="eqn-palette-tabs" data-eqn-tabs></div>' +
          '<div data-eqn-palettes></div>' +
          '<div class="field">' +
            '<label for="eqn-latex">LaTeX</label>' +
            '<textarea id="eqn-latex" rows="3" spellcheck="false" ' +
              'placeholder="\\frac{P_{1}V_{1}}{T_{1}} = \\frac{P_{2}V_{2}}{T_{2}}"></textarea>' +
            '<p class="field-hint">Click a symbol above to add it, or type LaTeX directly. ' +
              "The preview shows exactly what students will see.</p>" +
          "</div>" +
          '<label>Preview</label>' +
          '<div class="eqn-preview" data-eqn-preview></div>' +
          '<label class="check mt1"><input type="checkbox" data-eqn-display> ' +
            "<span>Centre it on its own line<span class=\"check-note\">" +
            "Use this for a long equation; leave it off to keep the maths inside a sentence." +
            "</span></span></label>" +
        "</div>" +
        '<footer>' +
          '<button type="button" class="button" data-eqn-close>Cancel</button>' +
          '<button type="button" class="button button-primary" data-eqn-insert>Insert</button>' +
        "</footer>" +
      "</div>";

    document.body.appendChild(modal);

    var tabsHost = modal.querySelector("[data-eqn-tabs]");
    var palettesHost = modal.querySelector("[data-eqn-palettes]");
    var input = modal.querySelector("#eqn-latex");
    var preview = modal.querySelector("[data-eqn-preview]");
    var displayBox = modal.querySelector("[data-eqn-display]");

    var names = [
      ["basic", "Basic"], ["calculus", "Calculus"],
      ["greek", "Greek"], ["engineering", "Engineering"]
    ];

    for (var n = 0; n < names.length; n++) {
      (function (key, label, first) {
        var tab = document.createElement("button");
        tab.type = "button";
        tab.className = "eqn-palette-tab" + (first ? " active" : "");
        tab.textContent = label;
        tab.setAttribute("data-palette-for", key);
        tabsHost.appendChild(tab);

        var palette = document.createElement("div");
        palette.className = "eqn-palette" + (first ? " shown" : "");
        palette.setAttribute("data-palette", key);

        var entries = PALETTES[key];
        for (var e = 0; e < entries.length; e++) {
          (function (pair) {
            var button = document.createElement("button");
            button.type = "button";
            button.textContent = pair[1];
            button.title = pair[0];
            button.addEventListener("click", function () {
              insert(input, pair[0]);
              refresh();
            });
            palette.appendChild(button);
          })(entries[e]);
        }
        palettesHost.appendChild(palette);

        tab.addEventListener("click", function () {
          var allTabs = tabsHost.querySelectorAll(".eqn-palette-tab");
          for (var t = 0; t < allTabs.length; t++) allTabs[t].classList.remove("active");
          tab.classList.add("active");
          var allPalettes = palettesHost.querySelectorAll(".eqn-palette");
          for (var p = 0; p < allPalettes.length; p++) allPalettes[p].classList.remove("shown");
          palette.classList.add("shown");
        });
      })(names[n][0], names[n][1], n === 0);
    }

    function refresh() {
      var latex = input.value.trim();
      if (!latex) {
        preview.className = "eqn-preview";
        preview.innerHTML = '<span class="muted">Nothing to preview yet.</span>';
        return;
      }
      if (!katexReady()) {
        preview.className = "eqn-preview error";
        preview.textContent = "The equation renderer did not load. The LaTeX will still be saved.";
        return;
      }
      try {
        preview.className = "eqn-preview";
        preview.innerHTML = window.katex.renderToString(latex, {
          displayMode: displayBox.checked,
          throwOnError: true,
          strict: false
        });
      } catch (e) {
        preview.className = "eqn-preview error";
        preview.textContent = (e && e.message) ? e.message : "That LaTeX could not be read.";
      }
    }

    input.addEventListener("input", refresh);
    displayBox.addEventListener("change", refresh);

    var closers = modal.querySelectorAll("[data-eqn-close]");
    for (var c = 0; c < closers.length; c++) {
      closers[c].addEventListener("click", close);
    }
    modal.addEventListener("click", function (e) {
      if (e.target === modal) close();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && modal.classList.contains("shown")) close();
    });

    modal.querySelector("[data-eqn-insert]").addEventListener("click", function () {
      var latex = input.value.trim();
      if (!latex) { close(); return; }
      var wrapped = displayBox.checked ? "$$" + latex + "$$" : "$" + latex + "$";
      if (activeField) insert(activeField, wrapped);
      close();
    });

    dialog = {
      modal: modal,
      input: input,
      displayBox: displayBox,
      refresh: refresh
    };
    return dialog;
  }

  function openDialog(field) {
    var d = buildDialog();
    activeField = field;
    d.input.value = "";
    d.displayBox.checked = false;
    d.refresh();
    d.modal.classList.add("shown");
    setTimeout(function () { d.input.focus(); }, 30);
  }

  function close() {
    if (dialog) dialog.modal.classList.remove("shown");
    activeField = null;
  }

  /* ---- boot ------------------------------------------------------------- */

  function boot() {
    attachToolbars();
    renderMath(document.body);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.MechExam = window.MechExam || {};
  window.MechExam.renderMath = renderMath;
  window.MechExam.renderInto = renderInto;
  window.MechExam.attachEquationToolbars = attachToolbars;
})();
