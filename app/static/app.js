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

  // Stop double submits: the first click sends the form, later clicks are ignored (the server is idempotent too).
  document.querySelectorAll("form[data-once]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (form.dataset.sent) { e.preventDefault(); return; }
      form.dataset.sent = "1";
      setTimeout(function () {
        form.querySelectorAll("button[type=submit], button:not([type])").forEach(function (b) { b.classList.add("busy"); b.setAttribute("aria-busy", "true"); });
      }, 0);
      // allow a retry if the page is still here after 20 s (for example the network dropped)
      setTimeout(function () { delete form.dataset.sent; form.querySelectorAll(".busy").forEach(function (b) { b.classList.remove("busy"); }); }, 20000);
    });
  });

  // Checking pages: poll the status and reload as soon as the result is ready.
  var poll = document.querySelector("[data-poll]");
  if (poll && window.fetch) {
    var tries = 0;
    var tick = function () {
      tries += 1;
      fetch(poll.dataset.poll, { credentials: "same-origin", cache: "no-store" }).then(function (r) { return r.json(); }).then(function (j) {
        if (j.state && j.state !== "checking" && j.state !== "reading") { location.reload(); }
        else if (tries < 150) { setTimeout(tick, 1500); }
      }).catch(function () { if (tries < 150) setTimeout(tick, 3000); });
    };
    setTimeout(tick, 1200);
  }

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
