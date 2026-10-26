"""Browser dashboard. The JSON service root stays available for API clients."""
from datetime import date, timedelta
from typing import Any, Dict, List

from app.i18n import text
from app.posting import note_value
from app.ui import (
    chip,
    esc,
    month_status_label,
    month_title,
    page_href,
    parse_note_date,
    render_page,
    show_amount,
    show_date,
)


def _sync_line(lang, sync):
    # type: (str, Dict[str, Any]) -> str
    if sync.get("running"):
        return text(lang, "dash_sync_running")
    last = sync.get("last") or {}
    if not isinstance(last, dict):
        return text(lang, "dash_sync_none")
    finished = last.get("finished_at") or ""
    error = last.get("error") or ""
    if finished and error:
        return text(lang, "dash_sync_last_error", when=finished, error=error)
    if finished:
        return text(lang, "dash_sync_last", when=finished)
    return text(lang, "dash_sync_none")


def _due_rows(lang, payload):
    # type: (str, Dict[str, Any]) -> List[Dict[str, str]]
    today = date.today()
    horizon = today + timedelta(days=30)
    found = []  # type: List[Dict[str, str]]
    seen = set()
    for item in payload.get("pending") or []:
        due = parse_note_date(item.get("due"))
        if due is None or due < today or due > horizon:
            continue
        label = item.get("description") or ""
        key = (due.isoformat(), label)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            {
                "due": due.isoformat(),
                "label": label,
                "amount": show_amount(lang, item.get("amount")),
            }
        )
    for row in payload.get("dues") or []:
        due = parse_note_date(row.get("due"))
        if due is None or due < today or due > horizon:
            continue
        label = row.get("label") or ""
        key = (due.isoformat(), label)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            {
                "due": due.isoformat(),
                "label": label,
                "amount": show_amount(lang, row.get("amount")),
            }
        )
    found.sort(key=lambda row: (row["due"], row["label"]))
    return found[:5]


def _checklist_items(lang, checklist):
    # type: (str, Dict[str, Any]) -> List[str]
    lines = []  # type: List[str]
    grouped = checklist.get("blockers_by_kind") or {}
    for row in grouped.get("unsupported_bank_line") or []:
        lines.append(
            text(
                lang,
                "dash_line_item",
                date=show_date(lang, row.get("date")),
                amount=show_amount(lang, row.get("amount")),
                description=row.get("description") or "",
            )
        )
    for kind in ("missing_document", "missing_sales_invoice"):
        for row in grouped.get(kind) or []:
            lines.append(
                text(
                    lang,
                    "dash_task_item",
                    amount=show_amount(lang, row.get("amount")),
                    description=row.get("item") or "",
                )
            )
    return lines[:6]


def render_home(payload, lang):
    # type: (Dict[str, Any], str) -> str
    month = payload["month"]
    checklist = payload["checklist"]
    status = checklist.get("status") or "missing"
    pending_count = len(payload.get("pending") or [])
    review = payload.get("review") or 0
    suspect = payload.get("suspect") or 0
    report_href = "/report/%s/view?lang=%s" % (month, lang)
    pending_href = page_href("/pending", month, lang)
    scan_href = page_href("/scan", month, lang)

    dues = _due_rows(lang, payload)
    if dues:
        due_html = "<ul class='dues'>%s</ul>" % "".join(
            "<li><strong>%s</strong> · %s<p>%s</p></li>"
            % (esc(show_date(lang, row["due"])), esc(row["amount"]), esc(row["label"]))
            for row in dues
        )
    else:
        due_html = "<p class='quiet'>%s</p>" % esc(text(lang, "dash_dues_empty"))

    clear_html = ""
    if pending_count == 0 and review == 0 and suspect == 0:
        clear_html = "<p>%s</p>" % esc(text(lang, "dash_clear"))

    if status == "closed":
        close_line = text(lang, "dash_closed")
    elif checklist.get("can_close"):
        close_line = text(lang, "dash_can_close")
    elif _checklist_items(lang, checklist):
        close_line = text(lang, "dash_blocked")
    else:
        close_line = text(lang, "dash_open")

    list_html = ""
    if status != "closed":
        items = _checklist_items(lang, checklist)
        if items:
            list_html = "<ul class='checklist'>%s</ul>" % "".join(
                "<li>%s</li>" % esc(item) for item in items
            )

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

    empty_docs = ""
    if not payload.get("documents"):
        empty_docs = "<p class='quiet'>%s</p>" % esc(text(lang, "dash_no_documents"))

    counts = text(
        lang,
        "dash_lines",
        lines=checklist.get("bank_lines"),
        missing=checklist.get("unsupported_bank_lines"),
        open=checklist.get("open_items"),
    )

    body = """
<section class="attention">
  <h1>%s</h1>
  %s
  <div class="stats">
    <a class="stat" href="%s"><strong>%s</strong><span>%s</span></a>
    <a class="stat" href="%s#reviews"><strong>%s</strong><span>%s</span></a>
    <a class="stat" href="%s#alerts"><strong>%s</strong><span>%s</span></a>
  </div>
  <h2>%s</h2>
  %s
</section>
<section class="card">
  <h2>%s %s</h2>
  <p>%s</p>
  <p class="quiet">%s</p>
  %s
  %s
  %s
</section>
<section class="card">
  <h2>%s</h2>
  <div class="actions">
    <a class="btn" href="%s">%s</a>
    <button type="button" id="sync" data-syncing="%s" data-failed="%s" data-offline="%s">%s</button>
    <a class="btn btn-ghost span-2" href="%s">%s</a>
  </div>
  <p id="sync-status" class="quiet">%s</p>
</section>
""" % (
        esc(text(lang, "dash_title")),
        clear_html,
        esc(pending_href),
        esc(pending_count),
        esc(text(lang, "dash_pending")),
        esc(report_href),
        esc(review),
        esc(text(lang, "dash_review")),
        esc(report_href),
        esc(suspect),
        esc(text(lang, "dash_suspect")),
        esc(text(lang, "dash_dues")),
        due_html,
        esc(text(lang, "dash_month")),
        chip(lang, "validated" if status == "closed" else "pending", ""),
        esc(close_line),
        esc(counts),
        empty_docs,
        list_html,
        close_button,
        esc(text(lang, "dash_quick")),
        esc(scan_href),
        esc(text(lang, "dash_scan")),
        esc(text(lang, "dash_syncing")),
        esc(text(lang, "dash_sync_failed")),
        esc(text(lang, "error_offline")),
        esc(text(lang, "dash_sync")),
        esc(report_href),
        esc(text(lang, "dash_report")),
        esc(_sync_line(lang, payload.get("sync") or {})),
    )
    # The month chip should say Open or Closed, not Validated/Pending.
    body = body.replace(
        chip(lang, "validated" if status == "closed" else "pending", ""),
        "<span class='chip chip-%s'>%s</span>"
        % (
            "resolved" if status == "closed" else "pending",
            esc(month_status_label(lang, status if status in ("open", "closed") else "open")),
        ),
        1,
    )
    title = "%s — %s" % (month_title(lang, month), "MonthClose")
    return render_page(
        lang,
        "dashboard",
        month,
        payload.get("months") or [month],
        title,
        body,
        "/",
    )


def document_due(document):
    # type: (object) -> object
    return parse_note_date(note_value(document.notes, "due_date"))
