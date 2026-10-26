/* Shared page behaviour. No frameworks. */
(function () {
  var root = document.documentElement;
  var lang = root.getAttribute("data-lang") || "pt";

  var monthSelect = document.getElementById("month");
  if (monthSelect) {
    monthSelect.addEventListener("change", function () {
      var month = monthSelect.value;
      var mode = monthSelect.getAttribute("data-month-nav");
      if (mode === "report") {
        window.location = "/report/" + encodeURIComponent(month) + "/view?lang=" + encodeURIComponent(lang);
        return;
      }
      var path = window.location.pathname || "/";
      window.location = path + "?month=" + encodeURIComponent(month) + "&lang=" + encodeURIComponent(lang);
    });
  }

  var syncButton = document.getElementById("sync");
  var syncStatus = document.getElementById("sync-status");
  if (syncButton && syncStatus) {
    syncButton.addEventListener("click", function () {
      syncButton.disabled = true;
      syncStatus.textContent = syncButton.getAttribute("data-syncing") || "";
      fetch("/sync", {method: "POST", headers: {Accept: "application/json"}})
        .then(function (response) {
          return response.json().then(function (body) {
            return {ok: response.ok, body: body};
          });
        })
        .then(function (result) {
          if (!result.ok) {
            syncStatus.textContent = syncButton.getAttribute("data-failed") || "";
            syncButton.disabled = false;
            return;
          }
          window.location.reload();
        })
        .catch(function () {
          syncStatus.textContent = syncButton.getAttribute("data-offline") || syncButton.getAttribute("data-failed") || "";
          syncButton.disabled = false;
        });
    });
  }

  var closeButton = document.getElementById("close-month");
  if (closeButton) {
    closeButton.addEventListener("click", function () {
      closeButton.disabled = true;
      var status = document.getElementById("close-status");
      if (status) status.textContent = closeButton.getAttribute("data-closing") || "";
      fetch(closeButton.getAttribute("data-url"), {
        method: "POST",
        headers: {Accept: "application/json", "Content-Type": "application/json"},
        body: "{}"
      })
        .then(function (response) {
          if (!response.ok) throw new Error("close");
          window.location.reload();
        })
        .catch(function () {
          closeButton.disabled = false;
          if (status) status.textContent = closeButton.getAttribute("data-failed") || "";
        });
    });
  }

  var printButton = document.getElementById("print-report");
  if (printButton) {
    printButton.addEventListener("click", function () {
      window.print();
    });
  }

  var form = document.getElementById("scan-form");
  if (!form) return;

  var strings = {};
  var stringsNode = document.getElementById("scan-strings");
  if (stringsNode) {
    try { strings = JSON.parse(stringsNode.textContent || "{}"); } catch (error) { strings = {}; }
  }
  var result = document.getElementById("result");
  var fileInput = document.getElementById("file");
  var noteInput = document.getElementById("note");
  var codeInput = document.getElementById("code");
  var submitButton = document.getElementById("scan-send");
  var lastError = "";
  var clock = null;
  var startedAt = 0;

  function say(key) {
    return strings[key] || key;
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"]/g, function (ch) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[ch];
    });
  }

  function elapsedSeconds() {
    return Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  }

  function stopClock() {
    if (clock) {
      clearInterval(clock);
      clock = null;
    }
  }

  function showError(code) {
    stopClock();
    lastError = code || "failed";
    var key = "error_" + lastError;
    if (!strings[key]) key = "error_failed";
    result.innerHTML =
      "<article class='card error-card' role='alert'>" +
      "<h2>" + esc(say(key)) + "</h2>" +
      "<button type='button' id='retry'>" + esc(say("retry")) + "</button>" +
      "</article>";
    var retry = document.getElementById("retry");
    if (retry) retry.addEventListener("click", onRetry);
    if (submitButton) submitButton.disabled = false;
  }

  function onRetry() {
    if (lastError === "bad_code" && codeInput) {
      codeInput.focus();
      return;
    }
    if (fileInput && fileInput.files && fileInput.files.length) {
      submitScan();
      return;
    }
    if (fileInput) fileInput.focus();
  }

  var stepOrder = ["uploading", "reading", "validating", "matching", "done"];

  function renderProgress(step) {
    var index = stepOrder.indexOf(step);
    if (index < 0) index = 0;
    var items = "";
    stepOrder.forEach(function (name, position) {
      if (name === "done" && step !== "done") return;
      var cls = "";
      if (position < index || step === "done") cls = "is-done";
      else if (position === index) cls = "is-current";
      items += "<li class='" + cls + "'>" + esc(say("step_" + name)) + "</li>";
    });
    var seconds = String(elapsedSeconds());
    var elapsed = say("scan_elapsed").replace("%(seconds)s", seconds);
    result.innerHTML =
      "<article class='card' role='status' aria-live='polite'>" +
      "<div class='progress-head'><span class='spinner' aria-hidden='true'></span>" +
      "<h2>" + esc(say("scan_progress")) + "</h2></div>" +
      "<ol class='steps'>" + items + "</ol>" +
      "<p class='quiet' id='elapsed'>" + esc(elapsed) + "</p>" +
      "</article>";
  }

  function tickElapsed() {
    var node = document.getElementById("elapsed");
    if (!node) return;
    var seconds = String(elapsedSeconds());
    node.textContent = say("scan_elapsed").replace("%(seconds)s", seconds);
  }

  function renderCards(cards) {
    stopClock();
    if (!cards || !cards.length) {
      showError("unreadable");
      return;
    }
    var broken = cards.length === 1 && cards[0].status === "error" && !cards[0].entity;
    if (broken) {
      showError("unreadable");
      return;
    }
    var html = cards.map(function (card) {
      return (
        "<article class='card result-card'>" +
        "<h2 class='doc-type'>" + esc(card.document_type || say("doc_other")) + "</h2>" +
        "<p class='entity'>" + esc(card.entity || "—") + "</p>" +
        "<p class='total'>" + esc(card.total || "—") + "</p>" +
        "<p class='when'>" + esc(card.date || "—") + "</p>" +
        "<p><span class='chip chip-" + esc(card.tone || "pending") + "'>" + esc(card.status_label || "") + "</span></p>" +
        (card.reason ? "<p class='reason'>" + esc(card.reason) + "</p>" : "") +
        "<p class='next-action'><strong>" + esc(say("pending_next")) + "</strong> " + esc(card.next_action || "") + "</p>" +
        "</article>"
      );
    }).join("");
    html +=
      "<div class='actions'>" +
      "<button type='button' id='scan-another'>" + esc(say("scan_another")) + "</button>" +
      "<a class='btn btn-ghost' href='" + esc(strings.recent_href || "/scan/recent") + "'>" + esc(say("see_recent")) + "</a>" +
      "</div>";
    result.innerHTML = html;
    var another = document.getElementById("scan-another");
    if (another) {
      another.addEventListener("click", function () {
        if (fileInput) fileInput.value = "";
        if (noteInput) noteInput.value = "";
        result.innerHTML = "";
        if (fileInput) fileInput.focus();
      });
    }
    if (submitButton) submitButton.disabled = false;
  }

  function prepareFile(file) {
    var name = file.name || "scan";
    var type = (file.type || "").toLowerCase();
    if (type === "application/pdf" || /\.pdf$/i.test(name)) {
      return Promise.resolve(file);
    }
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file);
      var image = new Image();
      image.onload = function () {
        var maxSide = 1600;
        var scale = Math.min(1, maxSide / Math.max(image.width, image.height));
        var width = Math.max(1, Math.round(image.width * scale));
        var height = Math.max(1, Math.round(image.height * scale));
        var canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        var context = canvas.getContext("2d");
        context.fillStyle = "#ffffff";
        context.fillRect(0, 0, width, height);
        context.drawImage(image, 0, 0, width, height);
        canvas.toBlob(function (blob) {
          URL.revokeObjectURL(url);
          if (!blob) {
            reject(new Error("compress"));
            return;
          }
          var base = name.replace(/\.[^.]+$/, "");
          resolve(new File([blob], base + ".jpg", {type: "image/jpeg"}));
        }, "image/jpeg", 0.8);
      };
      image.onerror = function () {
        URL.revokeObjectURL(url);
        reject(new Error("compress"));
      };
      image.src = url;
    });
  }

  function poll(id) {
    fetch("/scan/status/" + encodeURIComponent(id) + "?lang=" + encodeURIComponent(lang), {
      headers: {Accept: "application/json"}
    })
      .then(function (response) {
        return response.json().then(function (body) {
          return {ok: response.ok, body: body};
        });
      })
      .then(function (payload) {
        if (!payload.ok) {
          showError((payload.body && payload.body.error) || "unknown_job");
          return;
        }
        var step = payload.body.step || "reading";
        if (step === "error") {
          showError(payload.body.error || "unreadable");
          return;
        }
        if (step === "done") {
          renderCards(payload.body.results || []);
          return;
        }
        renderProgress(step);
        setTimeout(function () { poll(id); }, 700);
      })
      .catch(function () {
        showError("offline");
      });
  }

  function submitScan() {
    if (!fileInput || !fileInput.files || !fileInput.files.length) {
      if (fileInput) fileInput.focus();
      return;
    }
    if (submitButton) submitButton.disabled = true;
    startedAt = Date.now();
    stopClock();
    clock = setInterval(tickElapsed, 1000);
    renderProgress("uploading");
    prepareFile(fileInput.files[0])
      .then(function (file) {
        var data = new FormData();
        data.append("file", file, file.name);
        data.append("note", noteInput ? noteInput.value : "");
        data.append("code", codeInput ? codeInput.value : "");
        return fetch("/scan/upload", {
          method: "POST",
          body: data,
          headers: {Accept: "application/json"}
        });
      })
      .then(function (response) {
        return response.json().then(function (body) {
          return {ok: response.ok, body: body};
        });
      })
      .then(function (payload) {
        if (!payload.ok) {
          showError((payload.body && (payload.body.error || payload.body.detail)) || "failed");
          return;
        }
        renderProgress("reading");
        poll(payload.body.id);
      })
      .catch(function () {
        showError("offline");
      });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    submitScan();
  });
})();
