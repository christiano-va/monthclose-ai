"""Browser home. The JSON service root stays available for API clients."""
from typing import Any, Dict

from app.i18n import text
from app.posting import note_value
from app.present import agenda_rows, attention_counts, home_preview, render_agenda_row
from app.ui import (
    esc,
    month_name_year,
    month_status_label,
    page_href,
    parse_note_date,
    relative_time,
    render_page,
)


def _sync_label(lang, sync):
    # type: (str, Dict[str, Any]) -> str
    last = sync.get("last") or {}
    if not isinstance(last, dict) or not last.get("finished_at"):
        return text(lang, "sync_never")
    when = relative_time(lang, last.get("finished_at"))
    if last.get("error"):
        if when:
            return "%s · %s" % (text(lang, "sync_failed_short"), when)
        return text(lang, "sync_failed_short")
    if when:
        return text(lang, "sync_updated", when=when)
    return text(lang, "sync_never")


def render_home(session, payload, lang):
    # type: (object, Dict[str, Any], str) -> str
    month = payload["month"]
    checklist = payload["checklist"]
    status = checklist.get("status") or "missing"
    if status not in ("open", "closed"):
        status = "open"
    counts = attention_counts(session)
    pending_href = page_href("/pending", month, lang)
    review_href = page_href("/pending?focus=review", month, lang)
    suspect_href = page_href("/pending?focus=suspect", month, lang)
    agenda_href = page_href("/agenda", month, lang)
    preview = home_preview(agenda_rows(session, lang))
    if preview:
        week_html = "<ul class='agenda-list'>%s</ul>" % "".join(
            render_agenda_row(row, compact=True) for row in preview
        )
    else:
        week_html = "<p class='quiet'>%s</p>" % esc(text(lang, "home_week_empty"))

    if status == "closed":
        close_line = text(lang, "dash_closed")
    elif checklist.get("can_close"):
        close_line = text(lang, "dash_can_close")
    elif checklist.get("open_items") or checklist.get("unsupported_bank_lines"):
        close_line = text(lang, "dash_blocked")
    else:
        close_line = text(lang, "dash_open")

    close_button = ""
    if checklist.get("can_close") and status == "open":
        close_button = (
            "<button type='button' id='close-month' data-url='/periods/%s/close' "
            "data-closing='%s' data-failed='%s'>%s</button>"
            "<p id='close-status' class='quiet'></p>"
            % (
                esc(month),
                esc(text(lang, "dash_closing")),
                esc(text(lang, "dash_close_failed")),
                esc(text(lang, "dash_close")),
            )
        )

    figures = text(
        lang,
        "dash_lines",
        lines=checklist.get("bank_lines"),
        missing=checklist.get("unsupported_bank_lines"),
        open=checklist.get("open_items"),
    )
    chip = "<span class='chip chip-%s'>%s</span>" % (
        "resolved" if status == "closed" else "pending",
        esc(month_status_label(lang, status)),
    )
    running = (payload.get("sync") or {}).get("running")
    spinner = "<span class='spinner spinner-sm' aria-hidden='true'></span>" if running else "↻"
    sync_class = "sync-line is-syncing" if running else "sync-line"

    body = """
<h1>%s</h1>
<div class="stats">
  <a class="stat" href="%s"><strong>%s</strong><span>%s</span></a>
  <a class="stat" href="%s"><strong>%s</strong><span>%s</span></a>
  <a class="stat" href="%s"><strong>%s</strong><span>%s</span></a>
</div>
<section class="card">
  <div class="section-head">
    <h2>%s</h2>
    <a href="%s">%s</a>
  </div>
  %s
</section>
<section class="card">
  <div class="section-head">
    <h2>%s</h2>
    %s
  </div>
  <p>%s</p>
  <p class="quiet">%s</p>
  %s
</section>
<p class="%s"><button type="button" id="sync" data-syncing="%s" data-failed="%s">
  <span id="sync-label">%s</span>
  <span class="sync-mark" aria-hidden="true">%s</span>
</button></p>
""" % (
        esc(text(lang, "nav_home")),
        esc(pending_href),
        esc(counts["pending"]),
        esc(text(lang, "dash_pending")),
        esc(review_href),
        esc(counts["review"]),
        esc(text(lang, "dash_review")),
        esc(suspect_href),
        esc(counts["suspect"]),
        esc(text(lang, "dash_suspect")),
        esc(text(lang, "home_week")),
        esc(agenda_href),
        esc(text(lang, "home_week_link")),
        week_html,
        esc(text(lang, "home_close", month=month_name_year(lang, month))),
        chip,
        esc(close_line),
        esc(figures),
        close_button,
        sync_class,
        esc(text(lang, "dash_syncing")),
        esc(text(lang, "dash_sync_failed")),
        esc(_sync_label(lang, payload.get("sync") or {})),
        spinner,
    )
    title = "%s — MonthClose" % text(lang, "nav_home")
    return render_page(lang, "dashboard", month, payload.get("months") or [month], title, body, "/")


def document_due(document):
    # type: (object) -> object
    return parse_note_date(note_value(document.notes, "due_date"))
