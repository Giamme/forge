// Runs in <head> so the chosen theme applies before the first paint.
(function () {
  var choice = "system";
  try { choice = localStorage.getItem("forge-site-theme") || "system"; } catch (e) {}
  if (choice === "light" || choice === "dark") {
    document.documentElement.setAttribute("data-theme", choice);
  }
})();
