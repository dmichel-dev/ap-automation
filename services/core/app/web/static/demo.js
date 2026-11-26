// Public demo pages: refresh the cards while a file is being processed, and the Copy button.
// No inline code (the CSP allows this file only); the page works without it, by reloading.
(function () {
  "use strict";
  var REFRESH_MS = 4000;
  var MAX_REFRESHES = 150; // 10 minutes, then the visitor reloads by hand

  function startRefresh() {
    var box = document.getElementById("demo-items");
    if (!box) return;
    var url = box.getAttribute("data-refresh");
    var count = 0;
    function pending() {
      var cards = box.querySelector(".demo-cards");
      return cards && cards.getAttribute("data-pending") === "yes";
    }
    function tick() {
      if (!pending() || count++ >= MAX_REFRESHES) return;
      fetch(url, { credentials: "same-origin", headers: { Accept: "text/html" } })
        .then(function (r) {
          if (r.status === 404) { window.location.reload(); return null; } // the session ended
          return r.ok ? r.text() : null;
        })
        .then(function (html) {
          if (html !== null && html !== undefined) {
            var doc = new DOMParser().parseFromString(html, "text/html");
            var cards = doc.querySelector(".demo-cards");
            if (cards) box.replaceChildren(document.importNode(cards, true));
          }
          window.setTimeout(tick, REFRESH_MS);
        })
        .catch(function () { window.setTimeout(tick, REFRESH_MS * 2); });
    }
    window.setTimeout(tick, REFRESH_MS);
  }

  function copyButtons() {
    document.querySelectorAll("button[data-copy]").forEach(function (button) {
      var source = document.getElementById(button.getAttribute("data-copy"));
      if (!source || !navigator.clipboard) return;
      button.hidden = false;
      button.addEventListener("click", function () {
        navigator.clipboard.writeText(source.textContent.trim()).then(function () {
          button.textContent = "Copied";
          window.setTimeout(function () { button.textContent = "Copy"; }, 2000);
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    startRefresh();
    copyButtons();
  });
})();
