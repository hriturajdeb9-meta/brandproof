// Small progressive enhancements. Every action also works without JavaScript.
(function () {
  // Show a "checking" overlay for slow forms and stop double submits.
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function () {
      if (form.dataset.sent) return;
      form.dataset.sent = "1";
      var overlay = document.createElement("div");
      overlay.className = "busy-overlay";
      overlay.setAttribute("role", "status");
      overlay.innerHTML = '<div class="busy-box"><div class="spinner"></div><b></b><p class="small muted">Code checks run first; AI checks for claims and tone run in parallel and are then verified by code.</p></div>';
      overlay.querySelector("b").textContent = form.dataset.busy;
      document.body.appendChild(overlay);
      form.querySelectorAll("button[type=submit]").forEach(function (b) { b.classList.add("busy"); });
    });
  });

  // Confirm destructive actions.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // Link highlights and image boxes to their finding cards.
  function pulse(keys) {
    keys.forEach(function (k) {
      document.querySelectorAll('[data-keys~="' + k + '"]').forEach(function (el) {
        el.classList.add("pulse");
        setTimeout(function () { el.classList.remove("pulse"); }, 1200);
      });
    });
  }
  document.querySelectorAll(".hl[data-keys], .box[data-keys]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      var keys = el.dataset.keys.split(" ");
      var card = document.getElementById("f-" + keys[0]);
      if (card) {
        e.preventDefault();
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.add("flash-card");
        setTimeout(function () { card.classList.remove("flash-card"); }, 1400);
      }
    });
  });
  document.querySelectorAll(".fc[data-key]").forEach(function (card) {
    card.addEventListener("mouseenter", function () { pulse([card.dataset.key]); });
  });

  // Tabs on the draft page.
  document.querySelectorAll(".tab-head").forEach(function (head) {
    head.querySelectorAll(".tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        head.querySelectorAll(".tab").forEach(function (t) {
          t.classList.toggle("on", t === tab);
          t.setAttribute("aria-selected", t === tab ? "true" : "false");
          var panel = document.getElementById(t.dataset.tab);
          if (panel) panel.hidden = t !== tab;
        });
      });
    });
  });
  if (location.hash === "#comments") {
    var c = document.getElementById("comments");
    if (c) c.click();
  }

  // Keep the finding the user just acted on in view after the page reloads.
  if (location.hash.indexOf("#f-") === 0) {
    var card = document.querySelector(location.hash);
    if (card) { card.classList.add("flash-card"); setTimeout(function () { card.classList.remove("flash-card"); }, 1400); }
  }
})();
