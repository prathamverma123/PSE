/* Personal Search Engine — minimal vanilla JS, no external libraries.
   Handles: crawl job table auto-refresh, confirm dialogs, CSRF injection. */
(function () {
  "use strict";

  // ---- CSRF: inject the token into every form that posts ----------------
  function csrfToken() {
    // the token is rendered into each form server-side via session['_csrf'];
    // as a fallback, read it from any existing hidden input
    var el = document.querySelector('input[name="_csrf"]');
    return el ? el.value : "";
  }

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form || typeof form.appendChild !== "function") return;
    if (form.method && form.method.toUpperCase() !== "POST") return;
    if (form.querySelector('input[name="_csrf"]')) return;
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = "_csrf";
    input.value = csrfToken();
    form.appendChild(input);
  }, true);

  // ---- confirm delete ----------------------------------------------------
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest ? ev.target.closest("button.danger") : null;
    if (btn && !btn.dataset.confirmed) {
      if (!window.confirm("Are you sure?")) {
        ev.preventDefault();
        ev.stopImmediatePropagation();
      }
    }
  }, true);

  // ---- crawl job polling ---------------------------------------------------
  var jobsTable = document.getElementById("jobs-table");
  if (jobsTable) {
    var timer = null;

    function runningCount() {
      var rows = jobsTable.querySelectorAll("tr[data-job]");
      var n = 0;
      rows.forEach(function (r) {
        var b = r.querySelector(".badge");
        if (b && /running/i.test(b.textContent)) n++;
      });
      return n;
    }

    function refresh() {
      fetch("/crawl/status", { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data || !data.jobs) return;
          data.jobs.forEach(function (j) {
            var row = jobsTable.querySelector('tr[data-job="' + j.id + '"]');
            if (!row) { location.reload(); return; }
            row.querySelector(".progress").textContent =
              j.processed + "/" + j.total;
            row.querySelector(".indexed").textContent = j.indexed;
            row.querySelector(".skipped").textContent = j.skipped;
            row.querySelector(".errors").textContent = j.errors;
            var badge = row.querySelector(".badge");
            badge.textContent = j.status;
            badge.className = "badge badge-" + j.status;
            var msg = row.querySelector(".msg");
            if (msg && j.message) msg.textContent = j.message;
          });
          if (runningCount() === 0 && timer) {
            clearInterval(timer);
            timer = null;
          }
        })
        .catch(function () { /* offline or logged out — stop */ });
    }

    if (runningCount() > 0) {
      timer = setInterval(refresh, 2000);
    }
  }
})();
