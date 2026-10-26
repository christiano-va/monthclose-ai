"""Browser home page. The JSON service root stays available for API clients."""


def render_home():
    # type: () -> str
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MonthClose AI</title>
<style>
  body { margin: 0; padding: 16px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f4f1ea; color: #1c1917; }
  main { max-width: 40rem; margin: 0 auto; }
  h1 { font-size: 1.7rem; margin: 0 0 8px; }
  p { line-height: 1.45; }
  a { color: #1d4e4a; }
  button { margin-top: 8px; padding: 14px 18px; font-size: 1.05rem; font-weight: 700; color: white; background: #1d4e4a; border: 0; border-radius: 12px; cursor: pointer; }
  button:disabled { opacity: 0.6; cursor: wait; }
  pre { background: white; border-radius: 12px; padding: 16px; overflow: auto; }
</style>
</head>
<body>
<main>
  <h1>MonthClose AI</h1>
  <p>Read the MonthClose label, file documents in Drive, link bank lines, and prepare owner drafts. Nothing is sent.</p>
  <p><a href="/docs">API docs</a> · <a href="/scan">Phone scan</a></p>
  <button id="sync" type="button">Sync now</button>
  <pre id="summary">Loading status…</pre>
</main>
<script>
var button = document.getElementById("sync");
var summary = document.getElementById("summary");

function show(value) {
  summary.textContent = JSON.stringify(value, null, 2);
}

function loadStatus() {
  return fetch("/sync/status", {headers: {"Accept": "application/json"}})
    .then(function (response) { return response.json(); })
    .then(show)
    .catch(function () { summary.textContent = "Status is unavailable."; });
}

button.addEventListener("click", function () {
  button.disabled = true;
  summary.textContent = "Syncing…";
  fetch("/sync", {method: "POST", headers: {"Accept": "application/json"}})
    .then(function (response) {
      return response.json().then(function (body) {
        return {ok: response.ok, body: body};
      });
    })
    .then(function (result) {
      show(result.ok ? result.body : {error: result.body});
    })
    .catch(function () { summary.textContent = "Sync failed."; })
    .then(function () { button.disabled = false; });
});

loadStatus();
</script>
</body>
</html>
"""
