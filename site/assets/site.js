/* forge website: the theme bank, copy keys on code, and the docs route map's inspector. */
(function () {
  "use strict";

  /* ---------- theme: system by default, light or dark on request ---------- */
  var root = document.documentElement;
  var themeKeys = document.querySelectorAll("[data-theme-choice]");
  function showTheme() {
    var choice = root.getAttribute("data-theme") || "system";
    themeKeys.forEach(function (b) { b.setAttribute("aria-pressed", String(b.getAttribute("data-theme-choice") === choice)); });
  }
  themeKeys.forEach(function (b) {
    b.addEventListener("click", function () {
      var choice = b.getAttribute("data-theme-choice");
      if (choice === "system") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", choice);
      try { localStorage.setItem("forge-site-theme", choice); } catch (e) { /* storage may be off */ }
      showTheme();
    });
  });
  showTheme();

  /* ---------- copy keys: on code blocks and on [data-copy] buttons ---------- */
  function copyText(text, button, label) {
    var done = function (ok) {
      button.textContent = ok ? "Copied" : "Select and copy";
      button.setAttribute("data-state", ok ? "copied" : "failed");
      setTimeout(function () { button.textContent = label; button.removeAttribute("data-state"); }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    else done(false);
  }
  document.querySelectorAll("pre").forEach(function (pre) {
    if (pre.hasAttribute("data-nocopy")) return;
    var b = document.createElement("button");
    b.type = "button"; b.className = "copy"; b.textContent = "Copy"; b.setAttribute("aria-label", "Copy this code");
    b.addEventListener("click", function () {
      var clone = pre.cloneNode(true);
      clone.querySelectorAll(".copy, .p, .out").forEach(function (n) { n.remove(); });
      copyText(clone.textContent.replace(/\n{2,}/g, "\n").trim(), b, "Copy");
    });
    pre.appendChild(b);
  });
  document.querySelectorAll("[data-copy]").forEach(function (b) {
    var label = b.textContent;
    b.addEventListener("click", function () { copyText(b.getAttribute("data-copy"), b, label); });
  });

  /* ---------- docs index: the route map sets a route and opens its station in the inspector ---------- */
  var map = document.querySelector("[data-routemap]");
  if (!map) return;
  var inspector = document.querySelector("[data-inspector]");
  var stations = map.querySelectorAll(".stn");
  var tracks = map.querySelectorAll(".trk");
  function select(id) {
    var tpl = document.getElementById("stn-" + id);
    if (!tpl) return;
    inspector.replaceChildren(tpl.content.cloneNode(true));
    var route = (map.querySelector('.stn[data-id="' + id + '"]').getAttribute("data-route") || "").split(" ");
    stations.forEach(function (s) {
      var sid = s.getAttribute("data-id");
      s.classList.toggle("is-sel", sid === id);
      s.classList.toggle("is-route", route.indexOf(sid) >= 0);
      s.setAttribute("aria-pressed", String(sid === id));
    });
    tracks.forEach(function (t) {
      var ends = (t.getAttribute("data-ends") || "").split(" ");
      t.classList.toggle("is-route", ends.length === 2 && (route.indexOf(ends[0]) >= 0 || ends[0] === id) && (route.indexOf(ends[1]) >= 0 || ends[1] === id));
    });
    map.classList.add("has-route");
    try { history.replaceState(null, "", "#" + id); } catch (e) { /* sandboxed frame */ }
  }
  stations.forEach(function (s) {
    s.addEventListener("click", function () { select(s.getAttribute("data-id")); });
    s.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); select(s.getAttribute("data-id")); } });
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { map.classList.remove("has-route"); stations.forEach(function (s) { s.classList.remove("is-route"); }); tracks.forEach(function (t) { t.classList.remove("is-route"); }); }
  });
  var start = (location.hash || "").slice(1);
  select(document.getElementById("stn-" + start) ? start : "plan");
})();
