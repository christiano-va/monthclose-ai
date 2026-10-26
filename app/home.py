"""Browser dashboard. The JSON service root stays available for API clients."""
import html
from typing import Any, Dict, List


def _esc(value):
    # type: (object) -> str
    return html.escape("" if value is None else str(value), quote=True)


def render_home(payload):
    # type: (Dict[str, Any]) -> str
    month = payload["month"]
    options = []  # type: List[str]
    for name in payload["months"]:
        selected = " selected" if name == month else ""
        options.append("<option value='%s'%s>%s</option>" % (_esc(name), selected, _esc(name)))
    if not options:
        options.append("<option value='%s' selected>%s</option>" % (_esc(month), _esc(month)))

    checklist = payload["checklist"]
    status = checklist.get("status") or "missing"
    badge = status.replace("_", " ")
    blockers = []
    for text in checklist.get("blockers") or []:
        if text == "period is already closed":
            continue
        blockers.append("<li>%s</li>" % _esc(text))
    if status == "closed":
        close_line = "This month is closed."
    elif checklist.get("can_close"):
        close_line = "The close checklist is clear."
    elif blockers:
        close_line = "The month stays open until these are resolved or accepted."
    else:
        close_line = "The month is open."

    blocker_html = ""
    if blockers and status != "closed":
        blocker_html = "<ul>%s</ul>" % "".join(blockers)

    pending_rows = []
    for item in payload["pending"]:
        due = item.get("due") or ""
        due_html = " · due %s" % _esc(due) if due else ""
        pending_rows.append(
            "<li><span>%s</span> <strong>%s</strong>%s<p>%s</p></li>"
            % (
                _esc(item.get("kind")),
                _esc(item.get("amount")),
                due_html,
                _esc(item.get("description")),
            )
        )
    if pending_rows:
        pending_html = "<ul class='pending'>%s</ul>" % "".join(pending_rows)
    else:
        pending_html = "<p>No open pending items for this month.</p>"

    sync = payload.get("sync") or {}
    if sync.get("running"):
        sync_line = "A sync is running."
    else:
        last = sync.get("last") or {}
        finished = last.get("finished_at") if isinstance(last, dict) else ""
        error = last.get("error") if isinstance(last, dict) else ""
        if finished and error:
            sync_line = "Last sync %s (%s)." % (finished, error)
        elif finished:
            sync_line = "Last sync %s." % finished
        else:
            sync_line = "No sync yet."

    counters = (
        ("Documents", payload["documents"]),
        ("Validated", payload["validated"]),
        ("Review", payload["review"]),
        ("Suspect", payload["suspect"]),
    )
    counter_html = []
    for label, value in counters:
        counter_html.append(
            "<div><span>%s</span><strong>%s</strong></div>" % (_esc(label), _esc(value))
        )

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MonthClose AI</title>
<style>
  body { margin: 0; padding: 16px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f4f1ea; color: #1c1917; }
  main { max-width: 40rem; margin: 0 auto; }
  h1 { font-size: 1.7rem; margin: 0 0 4px; }
  h2 { font-size: 1.05rem; margin: 0 0 8px; }
  p, li { line-height: 1.45; }
  a { color: #1d4e4a; }
  form label { display: block; font-size: .85rem; font-weight: 650; margin-bottom: 6px; }
  select { width: 100%%; min-height: 48px; font-size: 1rem; padding: 8px 12px; border-radius: 12px; border: 1px solid #d6d3d1; background: white; }
  section { background: white; border-radius: 16px; padding: 16px; margin-top: 12px; }
  .status { display: inline-block; padding: 4px 10px; border-radius: 999px; background: #e7f0ee; color: #1d4e4a; font-weight: 700; text-transform: capitalize; }
  .counters { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .counters div { background: #faf8f4; border-radius: 12px; padding: 12px; }
  .counters span { display: block; font-size: .8rem; color: #57534e; }
  .counters strong { font-size: 1.45rem; }
  ul { padding-left: 18px; margin: 8px 0 0; }
  .pending { list-style: none; padding: 0; }
  .pending li { padding: 10px 0; border-top: 1px solid #e7e5e4; }
  .pending li:first-child { border-top: 0; }
  .pending span { display: inline-block; font-size: .75rem; letter-spacing: .03em; text-transform: uppercase; color: #57534e; }
  .pending p { margin: 4px 0 0; }
  .actions { display: grid; gap: 8px; }
  .actions a, button { display: block; text-align: center; text-decoration: none; padding: 14px 18px; font-size: 1.05rem; font-weight: 700; border-radius: 12px; }
  .actions a { background: white; color: #1d4e4a; border: 1px solid #1d4e4a; }
  button { color: white; background: #1d4e4a; border: 0; cursor: pointer; width: 100%%; }
  button:disabled { opacity: 0.6; cursor: wait; }
  .quiet { color: #57534e; font-size: .9rem; }
  @media (min-width: 720px) {
    .counters { grid-template-columns: repeat(4, 1fr); }
    .actions { grid-template-columns: 1fr 1fr; }
    .actions button { grid-column: 1 / -1; }
  }
</style>
</head>
<body>
<main>
  <h1>MonthClose AI</h1>
  <p class="quiet">Close the month. Nothing is sent until you send it yourself.</p>
  <section>
    <form method="get" action="/">
      <label for="month">Month</label>
      <select id="month" name="month" onchange="this.form.submit()">
        %s
      </select>
    </form>
  </section>
  <section>
    <h2>Close checklist <span class="status">%s</span></h2>
    <p>%s</p>
    <p class="quiet">%s bank lines · %s without a document · %s open items</p>
    %s
  </section>
  <section>
    <h2>Documents</h2>
    <div class="counters">%s</div>
  </section>
  <section>
    <h2>Pending items</h2>
    %s
  </section>
  <section class="actions">
    <a href="/report/%s/view">Monthly report</a>
    <a href="/scan">Phone scan</a>
    <button id="sync" type="button">Sync now</button>
    <p id="sync-status" class="quiet">%s</p>
  </section>
</main>
<script>
var button = document.getElementById("sync");
var status = document.getElementById("sync-status");
button.addEventListener("click", function () {
  button.disabled = true;
  status.textContent = "Syncing…";
  fetch("/sync", {method: "POST", headers: {"Accept": "application/json"}})
    .then(function (response) {
      return response.json().then(function (body) {
        return {ok: response.ok, body: body};
      });
    })
    .then(function (result) {
      if (!result.ok) {
        var detail = result.body && result.body.detail ? result.body.detail : "Sync failed.";
        status.textContent = typeof detail === "string" ? detail : "Sync failed.";
        button.disabled = false;
        return;
      }
      window.location.reload();
    })
    .catch(function () {
      status.textContent = "Sync failed.";
      button.disabled = false;
    });
});
</script>
</body>
</html>
""" % (
        "".join(options),
        _esc(badge),
        _esc(close_line),
        _esc(checklist.get("bank_lines")),
        _esc(checklist.get("unsupported_bank_lines")),
        _esc(checklist.get("open_items")),
        blocker_html,
        "".join(counter_html),
        pending_html,
        _esc(month),
        _esc(sync_line),
    )
