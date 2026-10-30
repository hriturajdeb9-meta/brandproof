// Small progressive enhancements. Every action also works without JavaScript.
(function () {
  // Haptics: a short tap on phones that support it, on every button and switch.
  var buzz = function (ms) { try { if (navigator.vibrate) navigator.vibrate(ms || 8); } catch (e) {} };
  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest("button, .btn, .who-btn, .feat, .draft-list a, summary");
    if (t) buzz(t.classList.contains("btn-primary") ? 14 : 8);
  }, true);

  // Toasts slide away by themselves.
  document.querySelectorAll(".toast").forEach(function (t) {
    if (t.classList.contains("error")) buzz([20, 60, 20]);
    var close = function () { t.classList.add("hide"); setTimeout(function () { t.parentNode && t.parentNode.remove(); }, 260); };
    var x = t.querySelector(".toast-x"); if (x) x.addEventListener("click", close);
    setTimeout(close, t.classList.contains("error") ? 9000 : 5000);
  });

  // Landing page: reveal on scroll, a light that follows the mouse, a gentle tilt on the preview card.
  var reveals = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && reveals.length) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en, i) {
        if (en.isIntersecting) { setTimeout(function () { en.target.classList.add("in"); }, i * 70); io.unobserve(en.target); }
      });
    }, { threshold: 0.12 });
    reveals.forEach(function (el) { io.observe(el); });
    setTimeout(function () { reveals.forEach(function (el) { el.classList.add("in"); }); }, 2500);  // never leave anything hidden
  } else { reveals.forEach(function (el) { el.classList.add("in"); }); }
  var hero = document.querySelector(".hero2"), glow = document.querySelector(".hero2-glow"), tilt = document.querySelector(".tilt");
  if (hero && window.matchMedia && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    hero.addEventListener("mousemove", function (e) {
      var r = hero.getBoundingClientRect(), x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
      if (glow) { glow.style.setProperty("--mx", (x * 100) + "%"); glow.style.setProperty("--my", (y * 100) + "%"); }
      if (tilt) tilt.style.transform = "perspective(900px) rotateY(" + ((x - 0.5) * 8) + "deg) rotateX(" + ((0.5 - y) * 6) + "deg)";
    });
    hero.addEventListener("mouseleave", function () { if (tilt) tilt.style.transform = ""; });
  }

  // "Try an example" chips on the new-draft page.
  document.querySelectorAll("[data-fill]").forEach(function (b) {
    b.addEventListener("click", function () {
      var body = document.getElementById("body"), title = document.getElementById("title");
      if (body) { body.value = b.dataset.fill; body.focus(); }
      if (title && !title.value) title.value = b.dataset.title || "";
    });
  });

  // Close the "More" menu when clicking elsewhere.
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.more[open]").forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute("open"); });
  });

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

  // Instant feedback: a thin progress bar the moment a page or form is on its way.
  var bar = document.createElement("div");
  bar.className = "navbar-progress";
  document.body.appendChild(bar);
  var startBar = function () { bar.classList.remove("go"); void bar.offsetWidth; bar.classList.add("go"); };
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || a.target || a.hasAttribute("download")) return;
    var href = a.getAttribute("href");
    if (href.charAt(0) === "#" || a.origin !== location.origin || /\.pdf($|\?)/.test(href)) return;
    startBar();
  });
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;
    startBar();
    // Switching person: show the new person as selected straight away.
    var btn = e.submitter;
    if (btn && btn.classList.contains("who-btn")) {
      document.querySelectorAll(".who-btn.on").forEach(function (b) { b.classList.remove("on"); b.setAttribute("aria-pressed", "false"); });
      btn.classList.add("on"); btn.setAttribute("aria-pressed", "true");
    }
  });
  window.addEventListener("pageshow", function () { bar.classList.remove("go"); });

  var calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Scores and summary numbers count up when the page opens, and score meters fill.
  if (!calm) {
    document.querySelectorAll(".score-val, .stat-val").forEach(function (el) {
      var m = /^(\d+(?:\.\d+)?)(%?)$/.exec(el.textContent.trim());
      if (!m) return;
      var end = parseFloat(m[1]), dec = (m[1].split(".")[1] || "").length, suf = m[2], t0 = null;
      if (end === 0) return;
      var step = function (t) {
        if (t0 === null) t0 = t;
        var k = Math.min(1, (t - t0) / 900), e = 1 - Math.pow(1 - k, 3);
        el.textContent = (end * e).toFixed(dec) + suf;
        if (k < 1) requestAnimationFrame(step);
      };
      el.textContent = (0).toFixed(dec) + suf;
      requestAnimationFrame(step);
    });
    document.querySelectorAll(".score[style]").forEach(function (s) {
      var v = s.style.getPropertyValue("--s");
      s.style.setProperty("--s", "0");
      requestAnimationFrame(function () { requestAnimationFrame(function () { s.style.setProperty("--s", v); }); });
    });
  }

  // A soft light follows the pointer across cards you can click.
  if (!calm && window.matchMedia("(hover: hover)").matches) {
    document.querySelectorAll(".draft-list a, .feat, .persona button, .stat, .score, .how3-step, .kit-tabs a").forEach(function (el) { el.classList.add("spot"); });
    document.addEventListener("pointermove", function (e) {
      var el = e.target.closest && e.target.closest(".spot");
      if (!el) return;
      var r = el.getBoundingClientRect();
      el.style.setProperty("--px", (e.clientX - r.left) + "px");
      el.style.setProperty("--py", (e.clientY - r.top) + "px");
    }, { passive: true });
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
